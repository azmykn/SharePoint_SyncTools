"""Google Drive CLI login — saves OAuth token into the app SQLite database (see config/app_settings.json)."""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

CREDENTIALS_FILE = os.path.join(ROOT, "credentials.json")
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def main():
    print("=" * 55)
    print("  Google Drive - Login")
    print("=" * 55)

    if not os.path.exists(CREDENTIALS_FILE):
        print("\n[ERROR] credentials.json not found!")
        print(f"        Expected at: {CREDENTIALS_FILE}")
        print("\n        Please download it from Google Cloud Console:")
        print("        https://console.cloud.google.com/apis/credentials")
        input("\nPress Enter to exit...")
        sys.exit(1)

    from google_auth_oauthlib.flow import InstalledAppFlow

    print("\n[...] Opening browser for Google login...")
    print("      (If browser doesn't open, copy the URL shown below)\n")

    flow = InstalledAppFlow.from_client_secrets_file(
        CREDENTIALS_FILE,
        scopes=SCOPES,
    )

    creds = flow.run_local_server(port=0, open_browser=True)

    from sp_sync.db.store import get_store
    from sp_sync.config.settings import database_file_path

    get_store().set_gdrive_token_json(creds.to_json())

    print("\n[OK] Login successful!")
    print(f"     Token saved in SQLite: {database_file_path()}")
    print("\n     You can now open the main app at http://localhost:5000")
    input("\nPress Enter to exit...")


if __name__ == "__main__":
    main()
