"""
TTG Consulting Portal — Google Drive → Supabase Storage Uploader
----------------------------------------------------------------
Run: python upload_to_supabase.py

Prompts you to pick a Drive folder and a Supabase storage bucket path,
previews all PDFs and images found, then uploads each one to Supabase Storage
and records the result in a local sync log (supabase_sync_log.json) so
re-runs are idempotent.

Requirements:
    pip install python-dotenv google-api-python-client google-auth-httplib2 \
                google-auth-oauthlib supabase tqdm
"""

import os
import io
import json
import mimetypes
import tempfile
from pathlib import Path
from datetime import datetime

from env_config import require_env

# ── Google Drive ──────────────────────────────────────────────────────────────
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ── Supabase ──────────────────────────────────────────────────────────────────
from supabase import create_client, Client
from tqdm import tqdm

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION — set values in .env (copy from .env.example)
# ─────────────────────────────────────────────────────────────────────────────

SUPABASE_URL         = require_env("SUPABASE_URL")
SUPABASE_SERVICE_KEY = require_env("SUPABASE_SERVICE_KEY")
SUPABASE_BUCKET      = os.getenv("SUPABASE_BUCKET", "resources-public")

GOOGLE_OAUTH_CLIENT_FILE = os.getenv("GOOGLE_OAUTH_CLIENT_FILE", "client_secret.json")

# Local file that tracks what has already been uploaded (auto-created)
SYNC_LOG_FILE = "supabase_sync_log.json"

# File types to pick up
PDF_EXTENSIONS   = {".pdf"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"}
ALL_EXTENSIONS   = PDF_EXTENSIONS | IMAGE_EXTENSIONS

# Whether to make uploaded files publicly accessible
# True  = public URL (good for marketing images, downloadable PDFs)
# False = private, access via signed URLs (good for paid resources)
PUBLIC_UPLOAD = True

# ─────────────────────────────────────────────────────────────────────────────

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_sync_log() -> dict:
    if Path(SYNC_LOG_FILE).exists():
        with open(SYNC_LOG_FILE) as f:
            return json.load(f)
    return {}


def save_sync_log(log: dict):
    with open(SYNC_LOG_FILE, "w") as f:
        json.dump(log, f, indent=2)
    print(f"  ✓ Sync log saved → {SYNC_LOG_FILE}")


def get_drive_service():
    """Authenticate with Google Drive via OAuth (browser popup on first run)."""
    creds = None
    token_file = "token_drive.json"

    if Path(token_file).exists():
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(GOOGLE_OAUTH_CLIENT_FILE).exists():
                print(f"\n✗ Missing OAuth file: {GOOGLE_OAUTH_CLIENT_FILE}")
                print("  Download it from: https://console.cloud.google.com")
                print("  APIs & Services → Credentials → OAuth 2.0 Client IDs → Download JSON")
                exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(GOOGLE_OAUTH_CLIENT_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w") as f:
            f.write(creds.to_json())

    return build("drive", "v3", credentials=creds)


def list_folder_contents(service, folder_id: str) -> list[dict]:
    """List all files in a Drive folder (non-recursive)."""
    results = []
    page_token = None

    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name, mimeType, size, modifiedTime)",
            pageToken=page_token,
        ).execute()

        results.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return results


def list_subfolders(service, folder_id: str) -> list[dict]:
    """List immediate subfolders of a Drive folder."""
    resp = service.files().list(
        q=f"'{folder_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id, name)",
    ).execute()
    return resp.get("files", [])


def pick_folder(service) -> tuple[str, str]:
    """
    Interactively lets you paste a folder ID or pick from a root folder's subfolders.
    Returns (folder_id, folder_name).
    """
    print("\n─────────────────────────────────────────")
    print("  TTG → Supabase Storage Uploader")
    print("─────────────────────────────────────────")
    print("\nHow would you like to specify the Drive folder?")
    print("  1. Paste a folder ID directly")
    print("  2. Browse subfolders of a root folder")
    choice = input("\nChoice (1/2): ").strip()

    if choice == "1":
        folder_id = input("Paste Drive folder ID: ").strip()
        meta = service.files().get(fileId=folder_id, fields="name").execute()
        return folder_id, meta.get("name", folder_id)

    elif choice == "2":
        root_id = input("Paste root Drive folder ID: ").strip()
        subfolders = list_subfolders(service, root_id)

        if not subfolders:
            print("  No subfolders found. Uploading from root folder directly.")
            meta = service.files().get(fileId=root_id, fields="name").execute()
            return root_id, meta.get("name", root_id)

        print("\nSubfolders found:")
        for i, f in enumerate(subfolders, 1):
            print(f"  {i}. {f['name']}  ({f['id']})")
        print(f"  {len(subfolders)+1}. Use root folder itself")

        idx = int(input("\nSelect folder number: ").strip()) - 1
        if idx == len(subfolders):
            meta = service.files().get(fileId=root_id, fields="name").execute()
            return root_id, meta.get("name", root_id)
        chosen = subfolders[idx]
        return chosen["id"], chosen["name"]

    else:
        print("Invalid choice.")
        exit(1)


def pick_storage_path(folder_name: str) -> str:
    """
    Let user define where in the Supabase bucket to put the files.
    Suggests a default based on the Drive folder name.
    """
    default = folder_name.lower().replace(" ", "-")
    print(f"\nWhere in the '{SUPABASE_BUCKET}' bucket should files be stored?")
    print(f"  Default: {default}/")
    print("  Examples: dsa/pdfs/  |  maplebear/images/  |  resources/2025/")
    path = input(f"\nStorage path (press Enter for '{default}/'): ").strip()
    if not path:
        path = default
    # Normalise: strip leading/trailing slashes
    path = path.strip("/")
    return path


def filter_by_type(files: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split files into (pdfs, images), ignoring other types."""
    pdfs   = [f for f in files if Path(f["name"]).suffix.lower() in PDF_EXTENSIONS]
    images = [f for f in files if Path(f["name"]).suffix.lower() in IMAGE_EXTENSIONS]
    return pdfs, images


def download_to_bytes(service, file_id: str, filename: str) -> bytes:
    """Download a Drive file and return its bytes."""
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request, chunksize=5 * 1024 * 1024)
    done = False
    with tqdm(total=100, desc=f"  Downloading", unit="%", leave=False) as bar:
        last = 0
        while not done:
            status, done = downloader.next_chunk()
            if status:
                progress = int(status.progress() * 100)
                bar.update(progress - last)
                last = progress
    return buf.getvalue()


def upload_to_supabase(
    sb: Client,
    file_bytes: bytes,
    storage_path: str,
    filename: str,
) -> str:
    """
    Upload bytes to Supabase Storage.
    Returns the public URL (or storage path if private).
    """
    content_type, _ = mimetypes.guess_type(filename)
    if not content_type:
        content_type = "application/octet-stream"

    dest_path = f"{storage_path}/{filename}"

    # upsert=True overwrites if file already exists
    sb.storage.from_(SUPABASE_BUCKET).upload(
        path=dest_path,
        file=file_bytes,
        file_options={
            "content-type": content_type,
            "upsert": "true",
        },
    )

    if PUBLIC_UPLOAD:
        result = sb.storage.from_(SUPABASE_BUCKET).get_public_url(dest_path)
        return result
    else:
        return dest_path


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if "YOUR_PROJECT" in SUPABASE_URL or SUPABASE_SERVICE_KEY == "your_service_role_key":
        print("\n✗ Please set SUPABASE_URL and SUPABASE_SERVICE_KEY in your .env file.")
        exit(1)

    # ── Connect to Supabase ───────────────────────────────────────────────────
    print("\nConnecting to Supabase...")
    sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    print("  ✓ Supabase connected")

    # ── Connect to Drive ──────────────────────────────────────────────────────
    print("Connecting to Google Drive...")
    service = get_drive_service()
    print("  ✓ Drive authenticated")

    # ── Pick folder ───────────────────────────────────────────────────────────
    folder_id, folder_name = pick_folder(service)
    print(f"\n  Folder: {folder_name} ({folder_id})")

    # ── List files ────────────────────────────────────────────────────────────
    all_files = list_folder_contents(service, folder_id)
    pdfs, images = filter_by_type(all_files)
    target_files = pdfs + images

    if not target_files:
        print("\n  No PDF or image files found in this folder.")
        print(f"  Supported types: {', '.join(sorted(ALL_EXTENSIONS))}")
        return

    # ── Pick storage path ─────────────────────────────────────────────────────
    storage_path = pick_storage_path(folder_name)
    print(f"\n  Will upload to: {SUPABASE_BUCKET}/{storage_path}/")

    # ── Load sync log ─────────────────────────────────────────────────────────
    sync_log = load_sync_log()
    new_files = [f for f in target_files if f["id"] not in sync_log]
    already_synced = len(target_files) - len(new_files)

    # ── Preview ───────────────────────────────────────────────────────────────
    new_pdfs   = [f for f in new_files if Path(f["name"]).suffix.lower() in PDF_EXTENSIONS]
    new_images = [f for f in new_files if Path(f["name"]).suffix.lower() in IMAGE_EXTENSIONS]

    print(f"\n  Found {len(target_files)} file(s) — "
          f"{already_synced} already synced, {len(new_files)} new")
    print(f"    PDFs:   {len(new_pdfs)}")
    print(f"    Images: {len(new_images)}\n")

    if not new_files:
        print("  Nothing to upload. All files already synced.")
        return

    print("  Files to upload:")
    for i, f in enumerate(new_files, 1):
        ext  = Path(f["name"]).suffix.lower()
        kind = "PDF" if ext in PDF_EXTENSIONS else "IMG"
        size_kb = int(f.get("size", 0)) / 1000
        print(f"    {i}. [{kind}] {f['name']}  ({size_kb:.0f} KB)")

    confirm = input(f"\nUpload {len(new_files)} file(s) to Supabase? (y/n): ").strip().lower()
    if confirm != "y":
        print("  Cancelled.")
        return

    # ── Upload loop ───────────────────────────────────────────────────────────
    print()
    results = []
    for i, file in enumerate(new_files, 1):
        ext  = Path(file["name"]).suffix.lower()
        kind = "PDF" if ext in PDF_EXTENSIONS else "IMG"
        print(f"[{i}/{len(new_files)}] [{kind}] {file['name']}")
        try:
            file_bytes = download_to_bytes(service, file["id"], file["name"])
            url = upload_to_supabase(sb, file_bytes, storage_path, file["name"])

            sync_log[file["id"]] = {
                "filename":          file["name"],
                "synced_at":         datetime.utcnow().isoformat(),
                "supabase_url":      url,
                "storage_path":      f"{storage_path}/{file['name']}",
                "bucket":            SUPABASE_BUCKET,
                "drive_folder_id":   folder_id,
                "drive_folder_name": folder_name,
            }
            save_sync_log(sync_log)

            results.append({**file, "url": url, "status": "ok"})
            short_url = url if len(url) < 80 else url[:77] + "..."
            print(f"  ✓ {short_url}\n")

        except Exception as e:
            print(f"  ✗ Failed: {e}\n")
            results.append({**file, "status": "error", "error": str(e)})

    # ── Summary ───────────────────────────────────────────────────────────────
    ok  = [r for r in results if r["status"] == "ok"]
    err = [r for r in results if r["status"] == "error"]

    print("─────────────────────────────────────────")
    print(f"  Done. {len(ok)} uploaded, {len(err)} failed.")

    if ok:
        print("\n  Paste these into Supabase (resources table):")
        print(f"  {'Filename':<45} URL")
        print(f"  {'-'*45} {'-'*50}")
        for r in ok:
            print(f"  {r['name']:<45} {r['url']}")

    if err:
        print("\n  Failed files:")
        for r in err:
            print(f"  ✗ {r['name']}: {r.get('error')}")
    print()


if __name__ == "__main__":
    main()
