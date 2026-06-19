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

```bash
pip install google-api-python-client google-auth-httplib2 
```

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

**`upload_to_mux.py`** — top of file:
```python
MUX_TOKEN_ID     = "your_token_id"
MUX_TOKEN_SECRET = "your_token_secret"
```
Get these from: [Mux Dashboard](https://dashboard.mux.com) → Settings → API Keys

**`upload_to_supabase.py`** — top of file:
```python
SUPABASE_URL         = "https://YOUR_PROJECT.supabase.co"
SUPABASE_SERVICE_KEY = "your_service_role_key"
```
Get these from: Supabase Dashboard → Settings → API → `service_role` key (not `anon`)

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

---

## Sync Logs

| File | Contents |
|---|---|
| `mux_sync_log.json` | Drive file ID → Mux asset_id + playback_id |
| `supabase_sync_log.json` | Drive file ID → Supabase storage URL |

Keep these files. They prevent re-uploading the same file on future runs.

---

## After Uploading

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
