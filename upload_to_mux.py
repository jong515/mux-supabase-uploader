"""
TTG Consulting Portal — Google Drive → Mux Video Uploader
---------------------------------------------------------
Run: python upload_to_mux.py

Prompts you to pick a Drive folder, previews all videos found,
then uploads each one to Mux and records the result in a local
sync log (mux_sync_log.json) so re-runs are idempotent.

Requirements:
    pip install python-dotenv google-api-python-client google-auth-httplib2 \
                google-auth-oauthlib mux-python requests tqdm
"""

import os
import json
import time
import argparse
import tempfile
import requests
from pathlib import Path
from datetime import datetime

from env_config import require_env
from cli_prompts import prompt_input

# ── Google Drive ──────────────────────────────────────────────────────────────
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ── Mux ───────────────────────────────────────────────────────────────────────
import mux_python
from mux_python.rest import ApiException
from tqdm import tqdm

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION — set values in .env (copy from .env.example)
# ─────────────────────────────────────────────────────────────────────────────

MUX_TOKEN_ID     = require_env("MUX_TOKEN_ID")
MUX_TOKEN_SECRET = require_env("MUX_TOKEN_SECRET")

GOOGLE_OAUTH_CLIENT_FILE = os.getenv("GOOGLE_OAUTH_CLIENT_FILE", "client_secret.json")

# Local file that tracks what has already been uploaded (auto-created)
SYNC_LOG_FILE = "mux_sync_log.json"

# Mux playback policy: "public" (anyone with the URL) or "signed" (requires JWT)
PLAYBACK_POLICY = "public"

# Video file extensions to pick up from Drive
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v"}

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


def resolve_folder_id(service, folder_id: str) -> tuple[str, str]:
    folder_id = folder_id.strip()
    meta = service.files().get(fileId=folder_id, fields="name").execute()
    return folder_id, meta.get("name", folder_id)


def pick_folder(service, folder_id: str | None = None) -> tuple[str, str]:
    """
    Interactively lets you paste a folder ID or pick from a root folder's subfolders.
    Returns (folder_id, folder_name).
    """
    if folder_id:
        return resolve_folder_id(service, folder_id)

    print("\n─────────────────────────────────────────")
    print("  TTG → Mux Video Uploader")
    print("─────────────────────────────────────────")
    print("\nHow would you like to specify the Drive folder?")
    print("  1. Paste a folder ID directly")
    print("  2. Browse subfolders of a root folder")
    print("  Or run with: --folder-id YOUR_FOLDER_ID")
    choice = prompt_input("\nChoice (1/2): ", show_paste_hint=False)

    if choice == "1":
        folder_id = prompt_input("Drive folder ID: ")
        return resolve_folder_id(service, folder_id)

    elif choice == "2":
        root_id = prompt_input("Root Drive folder ID: ")
        subfolders = list_subfolders(service, root_id)

        if not subfolders:
            print("  No subfolders found. Uploading from root folder directly.")
            return resolve_folder_id(service, root_id)

        print("\nSubfolders found:")
        for i, f in enumerate(subfolders, 1):
            print(f"  {i}. {f['name']}  ({f['id']})")
        print(f"  {len(subfolders)+1}. Use root folder itself")

        idx = int(prompt_input("\nSelect folder number: ", show_paste_hint=False)) - 1
        if idx == len(subfolders):
            return resolve_folder_id(service, root_id)
        chosen = subfolders[idx]
        return chosen["id"], chosen["name"]

    else:
        print("Invalid choice.")
        exit(1)


def download_to_temp(service, file_id: str, filename: str) -> str:
    """Download a Drive file to a temp path, return the path."""
    request = service.files().get_media(fileId=file_id)
    suffix = Path(filename).suffix
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)

    downloader = MediaIoBaseDownload(tmp, request, chunksize=10 * 1024 * 1024)
    done = False
    with tqdm(total=100, desc=f"  Downloading", unit="%", leave=False) as bar:
        last = 0
        while not done:
            status, done = downloader.next_chunk()
            if status:
                progress = int(status.progress() * 100)
                bar.update(progress - last)
                last = progress

    tmp.close()
    return tmp.name


def upload_to_mux(tmp_path: str, filename: str) -> dict:
    """
    Upload a local video file to Mux.
    Returns dict with asset_id and playback_id.
    """
    configuration = mux_python.Configuration()
    configuration.username = MUX_TOKEN_ID
    configuration.password = MUX_TOKEN_SECRET

    uploads_api = mux_python.DirectUploadsApi(mux_python.ApiClient(configuration))
    assets_api  = mux_python.AssetsApi(mux_python.ApiClient(configuration))

    # Step 1: Create a direct upload URL
    create_req = mux_python.CreateUploadRequest(
        new_asset_settings=mux_python.CreateAssetRequest(
            playback_policies=[PLAYBACK_POLICY],
            meta=mux_python.AssetMetadata(title=Path(filename).stem),
        ),
        timeout=3600,
    )
    upload = uploads_api.create_direct_upload(create_req)
    upload_url = upload.data.url
    upload_id  = upload.data.id

    # Step 2: PUT the file bytes to the upload URL
    file_size = os.path.getsize(tmp_path)
    with open(tmp_path, "rb") as f:
        with tqdm(total=file_size, desc=f"  Uploading ", unit="B",
                  unit_scale=True, leave=False) as bar:
            def read_with_progress(size=65536):
                data = f.read(size)
                bar.update(len(data))
                return data

            # Stream upload
            resp = requests.put(
                upload_url,
                data=iter(read_with_progress, b""),
                headers={"Content-Type": "video/mp4"},
                stream=True,
            )
            resp.raise_for_status()

    # Step 3: Poll until the asset is ready
    print("  ⏳ Waiting for Mux to process...", end="", flush=True)
    for _ in range(60):  # wait up to 5 mins
        time.sleep(5)
        upload_status = uploads_api.get_direct_upload(upload_id)
        asset_id = upload_status.data.asset_id
        if asset_id:
            asset = assets_api.get_asset(asset_id)
            if asset.data.status == "ready":
                playback_id = asset.data.playback_ids[0].id
                print(" ready!")
                return {"asset_id": asset_id, "playback_id": playback_id}
            elif asset.data.status == "errored":
                print(" error!")
                raise Exception(f"Mux asset errored: {asset_id}")
        print(".", end="", flush=True)

    raise Exception("Timed out waiting for Mux asset to become ready")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Upload Google Drive videos to Mux.")
    parser.add_argument(
        "--folder-id",
        metavar="ID",
        help="Google Drive folder ID (skips interactive folder prompt)",
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip upload confirmation prompt",
    )
    args = parser.parse_args()

    if MUX_TOKEN_ID == "your_token_id" or MUX_TOKEN_SECRET == "your_token_secret":
        print("\n✗ Please set MUX_TOKEN_ID and MUX_TOKEN_SECRET in your .env file.")
        exit(1)

    # ── Connect to Drive ──────────────────────────────────────────────────────
    print("\nConnecting to Google Drive...")
    service = get_drive_service()
    print("  ✓ Authenticated")

    # ── Pick folder ───────────────────────────────────────────────────────────
    folder_id, folder_name = pick_folder(service, folder_id=args.folder_id)
    print(f"\n  Folder: {folder_name} ({folder_id})")

    # ── List files ────────────────────────────────────────────────────────────
    all_files = list_folder_contents(service, folder_id)
    videos = [f for f in all_files
              if Path(f["name"]).suffix.lower() in VIDEO_EXTENSIONS]

    if not videos:
        print("\n  No video files found in this folder.")
        print(f"  Supported types: {', '.join(VIDEO_EXTENSIONS)}")
        return

    # ── Load sync log ─────────────────────────────────────────────────────────
    sync_log = load_sync_log()
    new_videos = [v for v in videos if v["id"] not in sync_log]
    already_synced = len(videos) - len(new_videos)

    # ── Preview ───────────────────────────────────────────────────────────────
    print(f"\n  Found {len(videos)} video(s) — {already_synced} already synced, "
          f"{len(new_videos)} new\n")

    if not new_videos:
        print("  Nothing to upload. All videos already synced.")
        return

    print("  Files to upload:")
    for i, v in enumerate(new_videos, 1):
        size_mb = int(v.get("size", 0)) / 1_000_000
        print(f"    {i}. {v['name']}  ({size_mb:.1f} MB)")

    if args.yes:
        confirm = "y"
    else:
        confirm = prompt_input(
            f"\nUpload {len(new_videos)} file(s) to Mux? (y/n): ", show_paste_hint=False
        ).lower()
    if confirm != "y":
        print("  Cancelled.")
        return

    # ── Upload loop ───────────────────────────────────────────────────────────
    print()
    results = []
    for i, video in enumerate(new_videos, 1):
        print(f"[{i}/{len(new_videos)}] {video['name']}")
        tmp_path = None
        try:
            # Download from Drive
            tmp_path = download_to_temp(service, video["id"], video["name"])

            # Upload to Mux
            mux_result = upload_to_mux(tmp_path, video["name"])

            # Record in sync log
            sync_log[video["id"]] = {
                "filename":    video["name"],
                "synced_at":   datetime.utcnow().isoformat(),
                "mux_asset_id":    mux_result["asset_id"],
                "mux_playback_id": mux_result["playback_id"],
                "drive_folder_id": folder_id,
                "drive_folder_name": folder_name,
            }
            save_sync_log(sync_log)

            results.append({**video, **mux_result, "status": "ok"})
            print(f"  ✓ asset_id={mux_result['asset_id']}")
            print(f"    playback_id={mux_result['playback_id']}\n")

        except Exception as e:
            print(f"  ✗ Failed: {e}\n")
            results.append({**video, "status": "error", "error": str(e)})

        finally:
            if tmp_path and Path(tmp_path).exists():
                os.unlink(tmp_path)

    # ── Summary ───────────────────────────────────────────────────────────────
    ok  = [r for r in results if r["status"] == "ok"]
    err = [r for r in results if r["status"] == "error"]

    print("─────────────────────────────────────────")
    print(f"  Done. {len(ok)} uploaded, {len(err)} failed.")
    if ok:
        print("\n  Paste these into Supabase (videos table):")
        print(f"  {'Filename':<40} {'asset_id':<30} playback_id")
        print(f"  {'-'*40} {'-'*30} {'-'*30}")
        for r in ok:
            print(f"  {r['name']:<40} {r['mux_asset_id']:<30} {r['mux_playback_id']}")
    print()


if __name__ == "__main__":
    main()
