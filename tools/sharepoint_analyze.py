"""
SharePoint folder analyzer (REST probe).
Saves JSON in the project root (gitignored). Override with env:
  ANALYZE_SP_URL   — guest or full URL to parse (folder id in query)
  ANALYZE_SP_SITE  — site root, e.g. https://tenant-my.sharepoint.com/personal/user_tenant_onmicrosoft_com
  ANALYZE_SP_REL   — server-relative folder path, e.g. /personal/user_tenant_onmicrosoft_com/Documents
"""

import json
import os
import re
from datetime import datetime

import requests

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_FILE = os.path.join(PROJECT_ROOT, "sharepoint_analysis.json")

# Placeholder defaults — set ANALYZE_SP_URL, ANALYZE_SP_SITE, and ANALYZE_SP_REL for real runs.
DEFAULT_GUEST_URL = (
    "https://your-tenant-my.sharepoint.com/:f:/g/personal/"
    "user_yourtenant_onmicrosoft_com/REPLACE_WITH_FOLDER_ID?e=token"
)

DEFAULT_SITE_ROOT = (
    "https://your-tenant-my.sharepoint.com/personal/"
    "user_yourtenant_onmicrosoft_com"
)

DEFAULT_REL_PATH = os.environ.get(
    "ANALYZE_SP_REL",
    "/personal/user_yourtenant_onmicrosoft_com/Documents",
)


def analyze_sharepoint_link(url):
    """Probe SharePoint REST for a folder (no auth — best-effort)."""
    print(f"Analyzing URL: {url}")
    print("=" * 50)

    try:
        match = re.search(r"/([a-zA-Z0-9_-]+)\?", url)
        if not match:
            print("No folder id found in URL (expected a ?query segment)")
            return None

        folder_id = match.group(1)
        print(f"Folder id from URL: {folder_id}")

        base_url = os.environ.get("ANALYZE_SP_SITE", DEFAULT_SITE_ROOT).strip()
        rel = os.environ.get("ANALYZE_SP_REL", DEFAULT_REL_PATH).strip()
        if not rel.startswith("/"):
            rel = "/" + rel
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json;odata=verbose",
        }

        print("Calling SharePoint REST (no cookies — often 401/403 without auth)...")
        folder_url = f"{base_url}/_api/web/GetFolderByServerRelativeUrl('{rel}')"

        try:
            response = requests.get(folder_url, headers=headers, timeout=15)
            if response.status_code == 200:
                _ = response.json()
                print("SharePoint API responded OK")

                contents_url = (
                    f"{base_url}/_api/web/GetFolderByServerRelativeUrl('{rel}')/Folders"
                )
                contents_response = requests.get(contents_url, headers=headers, timeout=15)
                if contents_response.status_code == 200:
                    contents_data = contents_response.json()
                    folders = []
                    files = []

                    if "d" in contents_data and "results" in contents_data["d"]:
                        for item in contents_data["d"]["results"]:
                            meta = item.get("__metadata", {}).get("type")
                            if meta == "SP.Folder":
                                folders.append(
                                    {
                                        "name": item["Name"],
                                        "path": item["ServerRelativeUrl"],
                                        "modified": item.get("TimeLastModified", "Unknown"),
                                        "item_count": item.get("ItemCount", 0),
                                    }
                                )
                            elif meta == "SP.File":
                                files.append(
                                    {
                                        "name": item["Name"],
                                        "path": item["ServerRelativeUrl"],
                                        "size": item.get("Length", 0),
                                        "modified": item.get("TimeLastModified", "Unknown"),
                                    }
                                )

                    print(f"\nFolders ({len(folders)}):")
                    for i, folder in enumerate(folders[:10], 1):
                        print(f"   {i}. {folder['name']}")
                        print(f"      path: {folder['path']}")
                        print()

                    if len(folders) > 10:
                        print(f"   ... and {len(folders) - 10} more folders")

                    print(f"\nFiles ({len(files)}):")
                    for i, f in enumerate(files[:10], 1):
                        size_mb = f["size"] / (1024 * 1024)
                        print(f"   {i}. {f['name']} ({size_mb:.1f} MB)")
                        print()

                    if len(files) > 10:
                        print(f"   ... and {len(files) - 10} more files")

                    return {
                        "success": True,
                        "folders": folders,
                        "files": files,
                        "total_folders": len(folders),
                        "total_files": len(files),
                    }
                print("No folder contents returned")
                return {"success": False, "error": "No folder contents found"}
            print(f"Access failed: HTTP {response.status_code}")
            return {"success": False, "error": f"HTTP {response.status_code}"}

        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
            return {"success": False, "error": f"Network error: {e}"}
        except Exception as e:
            print(f"Error: {e}")
            return {"success": False, "error": str(e)}

    except Exception as e:
        print(f"Analysis error: {e}")
        return {"success": False, "error": str(e)}


def generate_download_configs(folders, _base_url):
    """Build .sp_config style entries (local_path under project downloads/)."""
    configs = []
    dl_root = os.path.join(PROJECT_ROOT, "downloads")
    for folder in folders:
        safe = folder["name"].replace(" ", "_")
        configs.append(
            {
                "name": folder["name"],
                "rel_url": folder["path"],
                "local_path": os.path.join(dl_root, safe),
                "description": f"auto — {folder['modified']}",
            }
        )
    return configs


def save_results(result, url):
    """Save analysis results to sharepoint_analysis.json in project root."""
    analysis_data = {
        "timestamp": str(datetime.now()),
        "source_url": url,
        "analysis": result,
        "generated_configs": [],
    }

    if result and result.get("success"):
        analysis_data["generated_configs"] = generate_download_configs(
            result["folders"], url
        )

    try:
        with open(RESULTS_FILE, "w", encoding="utf-8") as f:
            json.dump(analysis_data, f, ensure_ascii=False, indent=2)

        print(f"\nResults saved to: {RESULTS_FILE}")

        if result and result.get("success") and analysis_data["generated_configs"]:
            print(f"\nGenerated {len(analysis_data['generated_configs'])} suggested download configs:")
            for i, config in enumerate(analysis_data["generated_configs"], 1):
                print(f"   {i}. {config['name']}")
                print(f"      local_path: {config['local_path']}")

    except Exception as e:
        print(f"Error saving results: {e}")


def main():
    print("SharePoint Folder Analyzer")
    print("=" * 60)

    url = os.environ.get("ANALYZE_SP_URL", DEFAULT_GUEST_URL).strip()
    print(f"\nTarget URL:\n   {url}\n")

    result = analyze_sharepoint_link(url)
    if result is None:
        result = {"success": False, "error": "parse failed"}

    save_results(result, url)

    print("\n" + "=" * 60)
    print("Analysis finished.")

    if result.get("success"):
        print("\nNext steps:")
        print("   1. Review sharepoint_analysis.json (gitignored by default)")
        print("   2. Copy the rel_url you need into the web UI (stored in SQLite)")
        print("   3. Run app.py and sync from the dashboard")
    else:
        print(f"\nNo content discovered: {result.get('error', 'Unknown')}")
        print("REST usually needs cookies — use the web app after signing in.")

    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
    except Exception as e:
        print(f"\nError: {e}")
        raise
