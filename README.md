# TTG Consulting Portal — Local Upload Scripts

Two local admin scripts for uploading content from Google Drive to your platforms.
Run from your own machine. Nothing is deployed or exposed to users.

---

## Scripts

| Script | Purpose |
|---|---|
| `upload_to_mux.py` | Google Drive → Mux (videos) |
| `upload_to_supabase.py` | Google Drive → Supabase Storage; course PDFs auto-register in `resources` table |

Each script keeps a local sync log so re-runs never double-upload the same file.

---

## One-Time Setup

### 1. Install dependencies

Use a virtual environment if you don't already have one:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install python-dotenv google-api-python-client google-auth-httplib2 `
            google-auth-oauthlib mux-python supabase requests tqdm
```

Requires **mux-python 5.x** (tested with 5.1.2).

### 2. Google OAuth credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a project (or use an existing one)
3. Enable the **Google Drive API**
4. Go to **APIs & Services → Credentials → Create Credentials → OAuth Client ID**(might need to enable consent first)
5. Application type: **Desktop app**
6. Download the JSON → save as `client_secret.json` in the same folder as the scripts

On first run a browser window will open asking you to sign in to Google.
After that a `token_drive.json` is saved locally so you won't be asked again.

### 3. Fill in credentials

Copy the example env file and add your keys:

```powershell
Copy-Item .env.example .env
```

Edit `.env`:

| Variable | Used by | Where to get it |
|---|---|---|
| `MUX_TOKEN_ID` | `upload_to_mux.py` | [Mux Dashboard](https://dashboard.mux.com) → Settings → API Keys |
| `MUX_TOKEN_SECRET` | `upload_to_mux.py` | Same as above |
| `SUPABASE_URL` | `upload_to_supabase.py` | Supabase Dashboard → Settings → API |
| `SUPABASE_SERVICE_KEY` | `upload_to_supabase.py` | `service_role` key (not `anon`) |
| `SUPABASE_BUCKET` | `upload_to_supabase.py` | Optional; used for **image** uploads in Drive mode (default: `resources-public`). Course PDF buckets are set automatically per course. |
| `GOOGLE_OAUTH_CLIENT_FILE` | Both scripts | Path to OAuth JSON (default: `client_secret.json`) |

Credentials are loaded by `env_config.py` from `.env` at startup. Never commit `.env` — only `.env.example` is tracked in git.

### 4. Local files (not in git)

| File | Purpose |
|---|---|
| `.env` | API keys and secrets |
| `client_secret.json` | Google OAuth client (from Cloud Console) |
| `token_drive.json` | Saved Google sign-in (created on first run) |
| `mux_sync_log.json` | Tracks uploaded videos |
| `supabase_sync_log.json` | Tracks uploaded PDFs/images (bucket, file_path, resource_id for PDFs) |

---

## How to Get a Drive Folder ID

Open the folder in Google Drive. The URL looks like:
```
https://drive.google.com/drive/folders/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs
                                        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                                        This is your folder ID
```

**Windows terminal paste:** Ctrl+V often types `^V` instead of pasting. Use **right-click → Paste** or **Shift+Insert**, or pass the folder ID on the command line (see below).

---

## Running the Scripts

```powershell
# Upload videos to Mux
python upload_to_mux.py

# Skip folder prompt (avoids paste issues in terminal)
python upload_to_mux.py --folder-id 1DNWt6NKk0w0tioxWn28bQjI_RN47_PHU -y

# Upload from Google Drive (PDFs + images)
python upload_to_supabase.py

# Drive upload with folder ID + course metadata on the command line
python upload_to_supabase.py --folder-id 1DNWt6NKk0w0tioxWn28bQjI_RN47_PHU --course 1 --topic dsa-pathways -y

# Upload local PDFs from a JSON manifest (no Drive)
python upload_to_supabase.py --manifest course_pdfs.json
```

### Course PDF upload (`upload_to_supabase.py`)

Course PDFs are uploaded to Supabase Storage **and** registered in the `resources` table so they appear in the TTG portal dashboard (`GET /api/v1/resources`).

**Storage layout (do not create new buckets):**

| Course | Bucket | Object key prefix | `is_paid` |
|---|---|---|---|
| Course 1 (free) | `resources-public` | `course-1/pdf/` | `false` |
| Course 2 (paid) | `resources-paid` | `course-2/pdf/` | `true` |

**Allowed `topic` values:**

| Topic | Course |
|---|---|
| `dsa-pathways` | Course 1 |
| `timelines-deadlines` | Course 1 |
| `interview-preparation` | Course 2 |

**Drive mode** (default):

1. Pick a Google Drive folder
2. Choose course (1 or 2) and topic — applies to all PDFs in the folder
3. PDFs upload to the correct bucket/path and upsert a `resources` row (`title`, `type`, `topic`, `bucket`, `file_path`, `is_paid`, etc.)
4. Images upload to Storage only (no `resources` row); path uses `SUPABASE_BUCKET` from `.env`

Optional companion file `pdfs_manifest.json` — array keyed by Drive `filename` to override `title`, `description`, `sort_order`, `duration`, or `file_path` per file. See field names in `course_pdfs.example.json`.

**Manifest mode** (`--manifest`):

Upload local PDFs from a JSON array. Copy `course_pdfs.example.json` as a template. Each entry needs:

- `local_path`, `bucket`, `file_path`, `title`, `topic`, `is_paid`
- Optional: `description`, `sort_order`, `duration`, `category`

Upload order: Storage first, then `resources` row. Re-runs are safe — Storage uses upsert; `resources` upserts on `(bucket, file_path)`.

**Summary output:** uploaded / registered / skipped / errors counts plus a per-file log.

### Mux playback access

By default, `upload_to_mux.py` uploads videos as **public** — anyone with the playback URL can stream them. This is controlled by `PLAYBACK_POLICY` at the top of `upload_to_mux.py`:

```python
PLAYBACK_POLICY = "public"   # or "signed" for JWT-protected playback
```

Only affects **new** uploads. Existing Mux assets keep the policy they were created with.

---

## Sync Logs

| File | Contents |
|---|---|
| `mux_sync_log.json` | Drive file ID → Mux asset_id + playback_id |
| `supabase_sync_log.json` | Drive file ID → bucket, file_path, resource_id (PDFs) or storage URL (images) |

Re-runs upsert Storage and `resources` rows; the sync log is an audit trail.

---

## After Uploading

**Videos (Mux)** — the script prints `asset_id` and `playback_id` for each file. Insert them into your videos table, for example:

```sql
INSERT INTO videos (mux_asset_id, mux_playback_id, title, entity)
VALUES ('abc123...', 'xyz789...', 'Introduction to DSA', 'dsa');
```

**Course PDFs (Supabase)** — registered automatically in `resources` by `upload_to_supabase.py`. No manual SQL needed. The portal reads `bucket` + `file_path` from the table to serve PDFs.

---

## Supported File Types

| Script | Extensions |
|---|---|
| Mux | `.mp4` `.mov` `.webm` `.avi` `.mkv` `.m4v` |
| Supabase | `.pdf` `.jpg` `.jpeg` `.png` `.webp` `.gif` `.svg` |
