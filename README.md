# TTG Consulting Portal — Local Upload Scripts

Two local admin scripts for uploading content from Google Drive to your platforms.
Run from your own machine. Nothing is deployed or exposed to users.

---

## Scripts

| Script | Purpose |
|---|---|
| `upload_to_mux.py` | Google Drive → Mux (videos) |
| `upload_to_supabase.py` | Google Drive → Supabase Storage (PDFs, images) |

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
| `SUPABASE_BUCKET` | `upload_to_supabase.py` | Storage bucket name (default: `resources-public`) |
| `GOOGLE_OAUTH_CLIENT_FILE` | Both scripts | Path to OAuth JSON (default: `client_secret.json`) |

Credentials are loaded by `env_config.py` from `.env` at startup. Never commit `.env` — only `.env.example` is tracked in git.

### 4. Local files (not in git)

| File | Purpose |
|---|---|
| `.env` | API keys and secrets |
| `client_secret.json` | Google OAuth client (from Cloud Console) |
| `token_drive.json` | Saved Google sign-in (created on first run) |
| `mux_sync_log.json` | Tracks uploaded videos |
| `supabase_sync_log.json` | Tracks uploaded PDFs/images |

---

## How to Get a Drive Folder ID

Open the folder in Google Drive. The URL looks like:
```
https://drive.google.com/drive/folders/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs
                                        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                                        This is your folder ID
```

---

## Running the Scripts

```bash
# Upload videos to Mux
python upload_to_mux.py

# Upload PDFs and images to Supabase
python upload_to_supabase.py
```

Both scripts will:
1. Ask you to specify a Drive folder (paste ID directly, or browse subfolders)
2. Show you a preview of all files found and what's new
3. Ask for confirmation before uploading anything
4. Print a summary with IDs/URLs to paste into Supabase

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
| `supabase_sync_log.json` | Drive file ID → Supabase storage URL |

Keep these files. They prevent re-uploading the same file on future runs.

---

## After Uploading

**Videos (Mux)** — the script prints `asset_id` and `playback_id` for each file. Insert them into your videos table, for example:

```sql
INSERT INTO videos (mux_asset_id, mux_playback_id, title, entity)
VALUES ('abc123...', 'xyz789...', 'Introduction to DSA', 'dsa');
```

**PDFs / Images (Supabase Storage)** — the script prints the public URL. Insert it:
```sql
INSERT INTO resources (public_url, file_type, entity, title)
VALUES ('https://...supabase.co/storage/...', 'pdf', 'dsa', 'Interview Prep Workbook');
```

---

## Supported File Types

| Script | Extensions |
|---|---|
| Mux | `.mp4` `.mov` `.webm` `.avi` `.mkv` `.m4v` |
| Supabase | `.pdf` `.jpg` `.jpeg` `.png` `.webp` `.gif` `.svg` |
