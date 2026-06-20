"""
TTG Consulting Portal — Google Drive → Supabase Storage + resources table
---------------------------------------------------------------------------
Run (Drive mode):
    python upload_to_supabase.py

Run (local manifest batch):
    python upload_to_supabase.py --manifest course_pdfs.json

Drive mode: pick a Drive folder, upload PDFs/images to Supabase Storage.
Course PDFs are registered in the `resources` table for the portal dashboard.
Images upload to Storage only (no resources row).

Requirements:
    pip install python-dotenv google-api-python-client google-auth-httplib2 \
                google-auth-oauthlib supabase tqdm
"""

import argparse
import io
import json
import mimetypes
import os
import re
from datetime import datetime
from pathlib import Path

from env_config import require_env
from cli_prompts import prompt_input
from resources_core import (
    ALLOWED_TOPICS,
    pick_course,
    pick_topic,
    process_pdf,
    validate_pdf_definition,
)
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from supabase import Client, create_client
from tqdm import tqdm

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION — set values in .env (copy from .env.example)
# ─────────────────────────────────────────────────────────────────────────────

SUPABASE_URL = require_env("SUPABASE_URL")
SUPABASE_SERVICE_KEY = require_env("SUPABASE_SERVICE_KEY")
# Legacy default for image uploads in Drive mode (optional override)
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "resources-public")
GOOGLE_OAUTH_CLIENT_FILE = os.getenv("GOOGLE_OAUTH_CLIENT_FILE", "client_secret.json")

SYNC_LOG_FILE = "supabase_sync_log.json"
DRIVE_MANIFEST_FILE = "pdfs_manifest.json"

PDF_EXTENSIONS = {".pdf"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"}
ALL_EXTENSIONS = PDF_EXTENSIONS | IMAGE_EXTENSIONS

# Whether image uploads use public URLs in sync log (images only; PDFs use bucket+file_path)
PUBLIC_UPLOAD = True

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


# ── Summary ───────────────────────────────────────────────────────────────────


class RunStats:
    def __init__(self):
        self.uploaded = 0
        self.registered = 0
        self.skipped = 0
        self.errors = 0
        self.log_lines: list[str] = []

    def record_ok(self, label: str, bucket: str, file_path: str, resource_id: str):
        self.uploaded += 1
        self.registered += 1
        self.log_lines.append(
            f"  ✓ {label} → {bucket}/{file_path} (id: {resource_id})"
        )

    def record_image_ok(self, label: str, bucket: str, dest_path: str):
        self.uploaded += 1
        self.log_lines.append(f"  ✓ {label} → {bucket}/{dest_path} (image, no resources row)")

    def record_skipped(self, label: str, reason: str):
        self.skipped += 1
        self.log_lines.append(f"  ⊘ {label} — {reason}")

    def record_error(self, label: str, reason: str):
        self.errors += 1
        self.log_lines.append(f"  ✗ {label} — {reason}")

    def print_summary(self):
        print("─────────────────────────────────────────")
        print("Done.")
        print(f"  Uploaded:   {self.uploaded}")
        print(f"  Registered: {self.registered}")
        print(f"  Skipped:    {self.skipped}")
        print(f"  Errors:     {self.errors}")
        if self.log_lines:
            print("\nPer-file log:")
            for line in self.log_lines:
                print(line)
        print()


# ── Manifest mode ─────────────────────────────────────────────────────────────


def load_manifest(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("Manifest must be a JSON array of PDF definitions")
    return data


def validate_manifest_all(entries: list[dict]) -> None:
    for i, entry in enumerate(entries, 1):
        try:
            validate_pdf_definition(entry)
        except ValueError as e:
            raise ValueError(f"Manifest entry {i}: {e}") from e
        local_path = entry.get("local_path")
        if not local_path:
            raise ValueError(f"Manifest entry {i}: missing local_path")
        if not Path(local_path).exists():
            raise ValueError(f"Manifest entry {i}: file not found: {local_path}")


def run_manifest_mode(sb: Client, manifest_path: str) -> None:
    print(f"\nLoading manifest: {manifest_path}")
    entries = load_manifest(manifest_path)
    if not entries:
        print("  Manifest is empty.")
        return

    validate_manifest_all(entries)
    print(f"  ✓ {len(entries)} entries validated")

    confirm = input(f"\nUpload {len(entries)} PDF(s) from manifest? (y/n): ").strip().lower()
    if confirm != "y":
        print("  Cancelled.")
        return

    stats = RunStats()
    print()
    for i, entry in enumerate(entries, 1):
        label = Path(entry["local_path"]).name
        print(f"[{i}/{len(entries)}] {label}")
        try:
            file_bytes = Path(entry["local_path"]).read_bytes()
            result = process_pdf(sb, file_bytes, entry)
            stats.record_ok(
                label, result["bucket"], result["file_path"], result["resource_id"]
            )
            print(f"  ✓ registered (id: {result['resource_id']})\n")
        except ValueError as e:
            stats.record_skipped(label, str(e))
            print(f"  ⊘ Skipped: {e}\n")
        except Exception as e:
            stats.record_error(label, str(e))
            print(f"  ✗ Failed: {e}\n")

    stats.print_summary()


# ── Drive helpers ─────────────────────────────────────────────────────────────


def load_sync_log() -> dict:
    if Path(SYNC_LOG_FILE).exists():
        with open(SYNC_LOG_FILE) as f:
            return json.load(f)
    return {}


def save_sync_log(log: dict):
    with open(SYNC_LOG_FILE, "w") as f:
        json.dump(log, f, indent=2)


def get_drive_service():
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
                exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(
                GOOGLE_OAUTH_CLIENT_FILE, SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(token_file, "w") as f:
            f.write(creds.to_json())

    return build("drive", "v3", credentials=creds)


def list_folder_contents(service, folder_id: str) -> list[dict]:
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
    resp = service.files().list(
        q=(
            f"'{folder_id}' in parents and "
            "mimeType='application/vnd.google-apps.folder' and trashed=false"
        ),
        fields="files(id, name)",
    ).execute()
    return resp.get("files", [])


def resolve_folder_id(service, folder_id: str) -> tuple[str, str]:
    folder_id = folder_id.strip()
    meta = service.files().get(fileId=folder_id, fields="name").execute()
    return folder_id, meta.get("name", folder_id)


def pick_folder(service, folder_id: str | None = None) -> tuple[str, str]:
    if folder_id:
        return resolve_folder_id(service, folder_id)

    print("\n─────────────────────────────────────────")
    print("  TTG → Supabase Storage Uploader")
    print("─────────────────────────────────────────")
    print("\nHow would you like to specify the Drive folder?")
    print("  1. Paste a folder ID directly")
    print("  2. Browse subfolders of a root folder")
    print("  Or run with: --folder-id YOUR_FOLDER_ID")
    choice = prompt_input("\nChoice (1/2): ", show_paste_hint=False)

    if choice == "1":
        folder_id = prompt_input("Drive folder ID: ")
        return resolve_folder_id(service, folder_id)

    if choice == "2":
        root_id = prompt_input("Root Drive folder ID: ")
        subfolders = list_subfolders(service, root_id)
        if not subfolders:
            return resolve_folder_id(service, root_id)
        print("\nSubfolders found:")
        for i, f in enumerate(subfolders, 1):
            print(f"  {i}. {f['name']}  ({f['id']})")
        print(f"  {len(subfolders) + 1}. Use root folder itself")
        idx = int(prompt_input("\nSelect folder number: ", show_paste_hint=False)) - 1
        if idx == len(subfolders):
            return resolve_folder_id(service, root_id)
        chosen = subfolders[idx]
        return chosen["id"], chosen["name"]

    print("Invalid choice.")
    exit(1)


def load_drive_manifest() -> dict[str, dict]:
    """Optional companion manifest keyed by Drive filename."""
    path = Path(DRIVE_MANIFEST_FILE)
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return {entry["filename"]: entry for entry in data if "filename" in entry}
    if isinstance(data, dict):
        return data
    return {}


def sanitize_filename(name: str) -> str:
    name = name.strip().replace(" ", "-")
    name = re.sub(r"[^\w.\-]", "", name, flags=re.ASCII)
    return name.lower()


def humanize_title(filename: str) -> str:
    stem = Path(filename).stem
    stem = stem.replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", stem).strip()


def build_drive_pdf_definition(
    drive_file: dict,
    course: dict,
    topic: str,
    sort_order: int,
    manifest_entry: dict | None,
) -> dict:
    filename = sanitize_filename(drive_file["name"])
    if not filename.lower().endswith(".pdf"):
        filename = f"{filename}.pdf" if not filename.endswith(".") else f"{filename}pdf"

    if manifest_entry and manifest_entry.get("file_path"):
        file_path = manifest_entry["file_path"].lstrip("/")
    else:
        file_path = f"{course['path_prefix']}{filename}"

    defn = {
        "bucket": course["bucket"],
        "file_path": file_path,
        "title": manifest_entry.get("title") if manifest_entry else humanize_title(drive_file["name"]),
        "topic": manifest_entry.get("topic", topic) if manifest_entry else topic,
        "is_paid": course["is_paid"],
        "category": course["category"],
        "sort_order": manifest_entry.get("sort_order", sort_order) if manifest_entry else sort_order,
    }
    if manifest_entry:
        for key in ("description", "duration"):
            if key in manifest_entry and manifest_entry[key] is not None:
                defn[key] = manifest_entry[key]
    return defn


def filter_by_type(files: list[dict]) -> tuple[list[dict], list[dict]]:
    pdfs = [f for f in files if Path(f["name"]).suffix.lower() in PDF_EXTENSIONS]
    images = [f for f in files if Path(f["name"]).suffix.lower() in IMAGE_EXTENSIONS]
    return pdfs, images


def download_to_bytes(service, file_id: str, filename: str) -> bytes:
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request, chunksize=5 * 1024 * 1024)
    done = False
    with tqdm(total=100, desc="  Downloading", unit="%", leave=False) as bar:
        last = 0
        while not done:
            status, done = downloader.next_chunk()
            if status:
                progress = int(status.progress() * 100)
                bar.update(progress - last)
                last = progress
    return buf.getvalue()


def upload_image_bytes(
    sb: Client,
    file_bytes: bytes,
    bucket: str,
    storage_path: str,
    filename: str,
) -> str:
    content_type, _ = mimetypes.guess_type(filename)
    if not content_type:
        content_type = "application/octet-stream"
    dest_path = f"{storage_path}/{filename}"
    sb.storage.from_(bucket).upload(
        path=dest_path,
        file=file_bytes,
        file_options={"content-type": content_type, "upsert": "true"},
    )
    if PUBLIC_UPLOAD:
        return sb.storage.from_(bucket).get_public_url(dest_path)
    return dest_path


def pick_image_storage_path(folder_name: str) -> str:
    default = folder_name.lower().replace(" ", "-")
    print(f"\nWhere in '{SUPABASE_BUCKET}' should images be stored?")
    path = prompt_input(f"Storage path (Enter for '{default}/'): ", show_paste_hint=False)
    return (path or default).strip("/")


def run_drive_mode(
    sb: Client,
    *,
    folder_id: str | None = None,
    course_arg: str | None = None,
    topic_arg: str | None = None,
    auto_confirm: bool = False,
) -> None:
    print("Connecting to Google Drive...")
    service = get_drive_service()
    print("  ✓ Drive authenticated")

    folder_id, folder_name = pick_folder(service, folder_id=folder_id)
    print(f"\n  Folder: {folder_name} ({folder_id})")

    all_files = list_folder_contents(service, folder_id)
    pdfs, images = filter_by_type(all_files)

    if not pdfs and not images:
        print("\n  No PDF or image files found in this folder.")
        return

    course = None
    topic = None
    drive_manifest = load_drive_manifest()
    if drive_manifest:
        print(f"\n  ✓ Loaded companion manifest: {DRIVE_MANIFEST_FILE}")

    image_storage_path = None
    if pdfs:
        course = pick_course(course_arg, content_label="PDFs")
        topic = pick_topic(course, topic_arg, content_label="PDFs")
        print(f"\n  PDFs → {course['bucket']}/{course['path_prefix']}*")
        print(f"  Topic: {topic}  |  Portal registration: yes")

    if images:
        image_storage_path = pick_image_storage_path(folder_name)
        print(f"\n  Images → {SUPABASE_BUCKET}/{image_storage_path}/")
        print("  Portal registration: no (images only)")

    target_files = pdfs + images
    print(f"\n  Found {len(pdfs)} PDF(s), {len(images)} image(s)")

    print("\n  Files to process:")
    for i, f in enumerate(target_files, 1):
        ext = Path(f["name"]).suffix.lower()
        kind = "PDF" if ext in PDF_EXTENSIONS else "IMG"
        size_kb = int(f.get("size", 0)) / 1000
        print(f"    {i}. [{kind}] {f['name']}  ({size_kb:.0f} KB)")

    if auto_confirm:
        confirm = "y"
    else:
        confirm = prompt_input(
            f"\nUpload {len(target_files)} file(s)? (y/n): ", show_paste_hint=False
        ).lower()
    if confirm != "y":
        print("  Cancelled.")
        return

    sync_log = load_sync_log()
    stats = RunStats()
    pdf_index = 0

    print()
    for i, file in enumerate(target_files, 1):
        ext = Path(file["name"]).suffix.lower()
        is_pdf = ext in PDF_EXTENSIONS
        kind = "PDF" if is_pdf else "IMG"
        print(f"[{i}/{len(target_files)}] [{kind}] {file['name']}")

        try:
            file_bytes = download_to_bytes(service, file["id"], file["name"])

            if is_pdf:
                pdf_index += 1
                manifest_entry = drive_manifest.get(file["name"])
                defn = build_drive_pdf_definition(
                    file, course, topic, pdf_index, manifest_entry
                )
                try:
                    validate_pdf_definition(defn)
                except ValueError as e:
                    stats.record_skipped(file["name"], str(e))
                    print(f"  ⊘ Skipped: {e}\n")
                    continue

                result = process_pdf(sb, file_bytes, defn)
                sync_log[file["id"]] = {
                    "filename": file["name"],
                    "synced_at": datetime.utcnow().isoformat(),
                    "bucket": result["bucket"],
                    "file_path": result["file_path"],
                    "resource_id": result["resource_id"],
                    "drive_folder_id": folder_id,
                    "drive_folder_name": folder_name,
                }
                save_sync_log(sync_log)
                stats.record_ok(
                    file["name"],
                    result["bucket"],
                    result["file_path"],
                    result["resource_id"],
                )
                print(f"  ✓ registered (id: {result['resource_id']})\n")

            else:
                dest_path = f"{image_storage_path}/{file['name']}"
                url = upload_image_bytes(
                    sb, file_bytes, SUPABASE_BUCKET, image_storage_path, file["name"]
                )
                sync_log[file["id"]] = {
                    "filename": file["name"],
                    "synced_at": datetime.utcnow().isoformat(),
                    "supabase_url": url,
                    "storage_path": dest_path,
                    "bucket": SUPABASE_BUCKET,
                    "drive_folder_id": folder_id,
                    "drive_folder_name": folder_name,
                }
                save_sync_log(sync_log)
                stats.record_image_ok(file["name"], SUPABASE_BUCKET, dest_path)
                print(f"  ✓ uploaded (no resources row)\n")

        except Exception as e:
            stats.record_error(file["name"], str(e))
            print(f"  ✗ Failed: {e}\n")

    stats.print_summary()


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Upload course PDFs to Supabase Storage and register in resources table."
    )
    parser.add_argument(
        "--manifest",
        metavar="JSON",
        help="Path to JSON manifest of local PDFs (skips Google Drive)",
    )
    parser.add_argument(
        "--folder-id",
        metavar="ID",
        help="Google Drive folder ID (skips interactive folder prompt)",
    )
    parser.add_argument(
        "--course",
        choices=["1", "2"],
        help="Course number for PDFs (use with --folder-id)",
    )
    parser.add_argument(
        "--topic",
        choices=sorted(ALLOWED_TOPICS),
        help="Resource topic for PDFs (use with --folder-id)",
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip upload confirmation prompt",
    )
    args = parser.parse_args()

    if "YOUR_PROJECT" in SUPABASE_URL or SUPABASE_SERVICE_KEY == "your_service_role_key":
        print("\n✗ Please set SUPABASE_URL and SUPABASE_SERVICE_KEY in your .env file.")
        exit(1)

    print("\nConnecting to Supabase...")
    sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    print("  ✓ Supabase connected")

    if args.manifest:
        run_manifest_mode(sb, args.manifest)
    else:
        run_drive_mode(
            sb,
            folder_id=args.folder_id,
            course_arg=args.course,
            topic_arg=args.topic,
            auto_confirm=args.yes,
        )


if __name__ == "__main__":
    main()
