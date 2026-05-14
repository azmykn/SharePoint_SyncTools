# Unified Sync Tool (SharePoint + Google Drive)

Local-first sync helpers for **SharePoint** (REST + optional Playwright for protected video) and **Google Drive** (OAuth), with a small **Flask** dashboard for cookies, folder targets, and logs.

## Author, repository, and license

- **Copyright © 2026 Azmy KN** — [azmykn@gmail.com](mailto:azmykn@gmail.com)
- **Repository (web):** [https://github.com/azmykn/SharePoint_SyncTools](https://github.com/azmykn/SharePoint_SyncTools)
- **Clone (SSH):** `git clone git@github.com:azmykn/SharePoint_SyncTools.git`
- **License:** [MIT](LICENSE) — you may use, copy, modify, and distribute the software with the copyright notice preserved; provided *as-is* without warranty.

## Requirements

- Python 3.11+ (recommended)
- `ffmpeg` on `PATH` (muxing browser-captured video/audio)

## Install

```bash
cd /path/to/SharePoint_SyncTools
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

Without `python -m playwright install chromium`, protected **video** capture may fail; plain REST downloads can still work for some files.

### Ubuntu 26.04 (and similar)

If you see `Playwright does not support chromium on ubuntu26.04-x64`, install system Chromium and point Playwright at it:

```bash
sudo apt install chromium
export PLAYWRIGHT_CHROMIUM_EXECUTABLE=/usr/bin/chromium
python app.py
```

The code also probes common paths such as `/usr/bin/chromium` when the variable is unset.

## Configuration (clear paths)

1. **Copy** `config/app_settings.example.json` → `config/app_settings.json` (the latter is gitignored).
2. Set at least:
   - **`sharepoint_site_url`** — tenant site root, e.g. `https://your-tenant-my.sharepoint.com/personal/user_tenant_onmicrosoft_com`
   - **`database_path`** (optional) — SQLite file; default is `data/app.sqlite` under the project root
   - **`default_guest_share_link`** (optional) — guest/share URL used to warm the browser session before REST

**Environment overrides** (highest priority for some keys):

| Variable | Purpose |
|----------|---------|
| `SP_SYNC_DB_PATH` | Full path to SQLite (overrides `database_path` in JSON) |
| `SP_SYNC_SITE_URL` | SharePoint site root (overrides `sharepoint_site_url`) |
| `SP_GUEST_LINK` | Guest/share link (overrides stored guest link) |

## Sensitive data — safe to publish on GitHub?

**You can open-source the repository** as long as you **do not commit**:

- `data/` (SQLite: cookies, OAuth token, configs, activity log)
- `credentials.json` (Google OAuth client secret JSON from Google Cloud Console)
- `config/app_settings.json` (your tenant URLs and paths)
- Any real cookies, tokens, guest links, or personal folder paths

The codebase should contain **only examples** (`*.example.json`, placeholders in `tools/sharepoint_analyze.py`). Before pushing, run `git status` and confirm none of the above are tracked.

## Run

### Web UI

From the project root:

```bash
python app.py
```

Open `http://127.0.0.1:5000`. Save SharePoint cookies under **Connection settings**, add folder targets, then sync.

### Google Drive CLI login (optional)

```bash
python gdrive_login.py
```

Stores the refresh token in the same SQLite database as the app (path from settings).

### Experimental REST probe

```bash
python tools/sharepoint_analyze.py
```

Set `ANALYZE_SP_URL`, `ANALYZE_SP_SITE`, and `ANALYZE_SP_REL` (see script docstring). Output: `sharepoint_analysis.json` (ignored by git if present).

### Local tests

```bash
python tests/test_enhancements.py
python tests/test_cookies.py
```

## Project layout

- `app.py` — launches the Flask UI
- `gdrive_login.py` — optional OAuth from the terminal
- `sp_sync/` — application package (`web`, `sharepoint`, `gdrive`, `db`, `config`, `security`)
- `templates/` — HTML (main UI is English by default; use **العربية** for RTL labels on the dashboard)
- `static/js/ui_locale.js` — English / Arabic toggle for the main page
- `config/app_settings.example.json` — template for local settings
- `config/sp_config.example.json` — example SharePoint row shape (reference only; live config is in SQLite)

## SharePoint guest link

Opening the guest link in Playwright first, then copying cookies into `requests`, improves some `_api` responses. Store the URL in SQLite (`sharepoint_guest_link`), in `app_settings.json`, or in `SP_GUEST_LINK`.

## Why is `.venv` huge?

The virtual environment contains full Python packages (Playwright, Google APIs, etc.). It is not application source. Delete `.venv` and recreate it anytime with `python -m venv .venv` and `pip install -r requirements.txt`.
