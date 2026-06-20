"""
TTG Consulting Portal — Google Drive → Mux Video Uploader + resources table
----------------------------------------------------------------------------
Run: python upload_to_mux.py

Prompts you to pick a Drive folder, previews all videos found,
registers each video in Supabase `resources`, uploads to Mux with the
resource UUID as passthrough, then updates the row with Mux IDs.

Requirements:
    pip install python-dotenv google-api-python-client google-auth-httplib2 \
                google-auth-oauthlib mux-python supabase requests tqdm
"""

import argparse
import json
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path

import mux_python
import requests
from env_config import require_env
from cli_prompts import prompt_input
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from supabase import Client, create_client
from tqdm import tqdm

from resources_core import (
    ALLOWED_TOPICS,
    build_video_resource_row,
    humanize_title,
    insert_video_resource,
    pick_course,
    pick_topic,
    update_video_mux_ids,
    validate_topic_for_course,
)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION — set values in .env (copy from .env.example)
# ─────────────────────────────────────────────────────────────────────────────

MUX_TOKEN_ID = require_env("MUX_TOKEN_ID")
MUX_TOKEN_SECRET = require_env("MUX_TOKEN_SECRET")
SUPABASE_URL = require_env("SUPABASE_URL")
SUPABASE_SERVICE_KEY = require_env("SUPABASE_SERVICE_KEY")

GOOGLE_OAUTH_CLIENT_FILE = os.getenv("GOOGLE_OAUTH_CLIENT_FILE", "client_secret.json")

SYNC_LOG_FILE = "mux_sync_log.json"
VIDEOS_MANIFEST_FILE = "videos_manifest.json"

# Mux playback policy: "public" (anyone with the URL) or "signed" (requires JWT)
PLAYBACK_POLICY = "public"

VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v"}

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


def load_videos_manifest() -> dict[str, dict]:
    path = Path(VIDEOS_MANIFEST_FILE)
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return {entry["filename"]: entry for entry in data if "filename" in entry}
    if isinstance(data, dict):
        return data
    return {}


def build_drive_video_definition(
    drive_file: dict,
    course: dict,
    topic: str,
    sort_order: int,
    manifest_entry: dict | None,
) -> dict:
    title = (
        manifest_entry.get("title")
        if manifest_entry and manifest_entry.get("title")
        else humanize_title(drive_file["name"])
    )
    video_topic = manifest_entry.get("topic", topic) if manifest_entry else topic

    defn = {
        "title": title,
        "topic": video_topic,
        "is_paid": course["is_paid"],
        "category": course["category"],
        "sort_order": (
            manifest_entry.get("sort_order", sort_order)
            if manifest_entry
            else sort_order
        ),
    }
    if manifest_entry:
        for key in ("description", "duration"):
            if key in manifest_entry and manifest_entry[key] is not None:
                defn[key] = manifest_entry[key]
    return defn


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

    if choice == "2":
        root_id = prompt_input("Root Drive folder ID: ")
        subfolders = list_subfolders(service, root_id)
        if not subfolders:
            print("  No subfolders found. Uploading from root folder directly.")
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


def download_to_temp(service, file_id: str, filename: str) -> str:
    request = service.files().get_media(fileId=file_id)
    suffix = Path(filename).suffix
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    downloader = MediaIoBaseDownload(tmp, request, chunksize=10 * 1024 * 1024)
    done = False
    with tqdm(total=100, desc="  Downloading", unit="%", leave=False) as bar:
        last = 0
        while not done:
            status, done = downloader.next_chunk()
            if status:
                progress = int(status.progress() * 100)
                bar.update(progress - last)
                last = progress
    tmp.close()
    return tmp.name


def upload_to_mux(tmp_path: str, filename: str, resource_id: str) -> dict:
    """Upload a local video to Mux with passthrough = resource UUID."""
    configuration = mux_python.Configuration()
    configuration.username = MUX_TOKEN_ID
    configuration.password = MUX_TOKEN_SECRET

    uploads_api = mux_python.DirectUploadsApi(mux_python.ApiClient(configuration))
    assets_api = mux_python.AssetsApi(mux_python.ApiClient(configuration))

    create_req = mux_python.CreateUploadRequest(
        new_asset_settings=mux_python.CreateAssetRequest(
            playback_policies=[PLAYBACK_POLICY],
            passthrough=resource_id,
            meta=mux_python.AssetMetadata(title=Path(filename).stem),
        ),
        timeout=3600,
    )
    upload = uploads_api.create_direct_upload(create_req)
    upload_url = upload.data.url
    upload_id = upload.data.id

    file_size = os.path.getsize(tmp_path)
    with open(tmp_path, "rb") as f:
        with tqdm(
            total=file_size, desc="  Uploading ", unit="B", unit_scale=True, leave=False
        ) as bar:
            def read_with_progress(size=65536):
                data = f.read(size)
                bar.update(len(data))
                return data

            resp = requests.put(
                upload_url,
                data=iter(read_with_progress, b""),
                headers={"Content-Type": "video/mp4"},
                stream=True,
            )
            resp.raise_for_status()

    print("  ⏳ Waiting for Mux to process...", end="", flush=True)
    for _ in range(60):
        time.sleep(5)
        upload_status = uploads_api.get_direct_upload(upload_id)
        asset_id = upload_status.data.asset_id
        if asset_id:
            asset = assets_api.get_asset(asset_id)
            if asset.data.status == "ready":
                playback_id = asset.data.playback_ids[0].id
                print(" ready!")
                return {"asset_id": asset_id, "playback_id": playback_id}
            if asset.data.status == "errored":
                print(" error!")
                raise Exception(f"Mux asset errored: {asset_id}")
        print(".", end="", flush=True)

    raise Exception("Timed out waiting for Mux asset to become ready")


def register_and_upload_video(
    sb: Client,
    service,
    video: dict,
    course: dict,
    topic: str,
    sort_order: int,
    manifest_entry: dict | None,
    sync_entry: dict | None,
    folder_id: str,
    folder_name: str,
) -> dict:
    defn = build_drive_video_definition(
        video, course, topic, sort_order, manifest_entry
    )
    validate_topic_for_course(defn["topic"], course)

    row = build_video_resource_row(defn)
    existing_id = sync_entry.get("resource_id") if sync_entry else None
    resource_id = insert_video_resource(sb, row, existing_id=existing_id)

    tmp_path = download_to_temp(service, video["id"], video["name"])
    try:
        mux_result = upload_to_mux(tmp_path, video["name"], resource_id)
    finally:
        if Path(tmp_path).exists():
            os.unlink(tmp_path)

    signed = PLAYBACK_POLICY == "signed"
    update_video_mux_ids(
        sb,
        resource_id,
        mux_result["asset_id"],
        mux_result["playback_id"],
        signed=signed,
    )

    sync_log = load_sync_log()
    sync_log[video["id"]] = {
        "filename": video["name"],
        "synced_at": datetime.utcnow().isoformat(),
        "resource_id": resource_id,
        "mux_asset_id": mux_result["asset_id"],
        "mux_playback_id": mux_result["playback_id"],
        "drive_folder_id": folder_id,
        "drive_folder_name": folder_name,
    }
    save_sync_log(sync_log)

    return {
        "status": "ok",
        "resource_id": resource_id,
        **mux_result,
    }


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Upload Google Drive videos to Mux and register in resources table."
    )
    parser.add_argument(
        "--folder-id",
        metavar="ID",
        help="Google Drive folder ID (skips interactive folder prompt)",
    )
    parser.add_argument(
        "--course",
        choices=["1", "2"],
        help="Course number for portal registration",
    )
    parser.add_argument(
        "--topic",
        choices=sorted(ALLOWED_TOPICS),
        help="Resource topic for portal registration",
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
    if "YOUR_PROJECT" in SUPABASE_URL or SUPABASE_SERVICE_KEY == "your_service_role_key":
        print("\n✗ Please set SUPABASE_URL and SUPABASE_SERVICE_KEY in your .env file.")
        exit(1)

    print("\nConnecting to Supabase...")
    sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    print("  ✓ Supabase connected")

    print("Connecting to Google Drive...")
    service = get_drive_service()
    print("  ✓ Drive authenticated")

    folder_id, folder_name = pick_folder(service, folder_id=args.folder_id)
    print(f"\n  Folder: {folder_name} ({folder_id})")

    all_files = list_folder_contents(service, folder_id)
    videos = [
        f for f in all_files if Path(f["name"]).suffix.lower() in VIDEO_EXTENSIONS
    ]

    if not videos:
        print("\n  No video files found in this folder.")
        print(f"  Supported types: {', '.join(sorted(VIDEO_EXTENSIONS))}")
        return

    course = pick_course(args.course, content_label="videos")
    topic = pick_topic(course, args.topic, content_label="videos")
    print(f"\n  Course: {course['category']}  |  Topic: {topic}")
    print("  Portal registration: yes (passthrough = resource UUID)")

    videos_manifest = load_videos_manifest()
    if videos_manifest:
        print(f"  ✓ Loaded companion manifest: {VIDEOS_MANIFEST_FILE}")

    sync_log = load_sync_log()
    previously_synced = sum(1 for v in videos if v["id"] in sync_log)

    print(f"\n  Found {len(videos)} video(s) — {previously_synced} previously synced")
    print("\n  Files to upload:")
    for i, v in enumerate(videos, 1):
        size_mb = int(v.get("size", 0)) / 1_000_000
        print(f"    {i}. {v['name']}  ({size_mb:.1f} MB)")

    if args.yes:
        confirm = "y"
    else:
        confirm = prompt_input(
            f"\nUpload {len(videos)} file(s) to Mux? (y/n): ", show_paste_hint=False
        ).lower()
    if confirm != "y":
        print("  Cancelled.")
        return

    print()
    results = []
    for i, video in enumerate(videos, 1):
        print(f"[{i}/{len(videos)}] {video['name']}")
        try:
            result = register_and_upload_video(
                sb,
                service,
                video,
                course,
                topic,
                i,
                videos_manifest.get(video["name"]),
                sync_log.get(video["id"]),
                folder_id,
                folder_name,
            )
            results.append({**video, **result})
            print(f"  ✓ resource_id={result['resource_id']}")
            print(f"    asset_id={result['asset_id']}")
            print(f"    playback_id={result['playback_id']}\n")
        except Exception as e:
            print(f"  ✗ Failed: {e}\n")
            results.append({**video, "status": "error", "error": str(e)})

    ok = [r for r in results if r.get("status") == "ok"]
    err = [r for r in results if r.get("status") == "error"]

    print("─────────────────────────────────────────")
    print(f"  Done. {len(ok)} uploaded, {len(err)} failed.")
    if ok:
        print(f"\n  {'Filename':<40} {'resource_id':<38} playback_id")
        print(f"  {'-'*40} {'-'*38} {'-'*30}")
        for r in ok:
            print(
                f"  {r['name']:<40} {r['resource_id']:<38} {r['playback_id']}"
            )
    if err:
        print("\n  Failed files:")
        for r in err:
            print(f"  ✗ {r['name']}: {r.get('error')}")
    print()


if __name__ == "__main__":
    main()
