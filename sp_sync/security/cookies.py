"""SharePoint cookie validation & live REST connectivity probe."""

from __future__ import annotations

import urllib.parse


class CookieValidator:
    """Validates SharePoint cookies for format and live session authenticity."""

    def __init__(self):
        self.sp_domain = "sharepoint.com"

    def validate_cookies(self, fedauth, rtfa):
        result = {"valid": False, "issues": [], "recommendations": []}

        if not fedauth:
            result["issues"].append("FedAuth cookie is missing")
            result["recommendations"].append("Extract FedAuth cookie from browser")

        if not rtfa:
            result["issues"].append("rtFa cookie is missing")
            result["recommendations"].append("Extract rtFa cookie from browser")

        if not fedauth or not rtfa:
            return result

        if len(fedauth) < 50:
            result["issues"].append("FedAuth cookie appears too short")
            result["recommendations"].append("Re-extract FedAuth cookie")

        if len(rtfa) < 20:
            result["issues"].append("rtFa cookie appears too short")
            result["recommendations"].append("Re-extract rtFa cookie")

        if fedauth.startswith("null") or "undefined" in fedauth.lower():
            result["issues"].append("FedAuth contains invalid values")
            result["recommendations"].append("Re-authenticate and extract fresh cookies")

        if rtfa.startswith("null") or "undefined" in rtfa.lower():
            result["issues"].append("rtFa contains invalid values")
            result["recommendations"].append("Re-authenticate and extract fresh cookies")

        if not result["issues"]:
            result["valid"] = True
            result["recommendations"].append("Cookies appear valid. Test with a live check or sync.")

        return result

    def test_cookie_connectivity(self, fedauth, rtfa, test_url=None):
        """Test cookies against SharePoint ``/_api/web/currentuser`` (JSON, no login-redirect following)."""
        site = ""
        if not test_url:
            try:
                from sp_sync.db.store import get_store

                site = (get_store().get_sharepoint_site_url() or "").strip().rstrip("/")
                if site:
                    test_url = f"{site}/_api/web/currentuser"
            except Exception:
                pass

        if not test_url:
            return {
                "success": False,
                "message": "SharePoint site URL is not set. Configure site URL first or provide a test URL.",
            }

        parsed = urllib.parse.urlparse(test_url)
        host = (parsed.netloc or "").lower()
        if parsed.scheme != "https" or self.sp_domain not in host:
            return {
                "success": False,
                "message": "Test URL must be an https://*.sharepoint.com URL.",
            }

        try:
            import requests

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json;odata=nometadata",
            }
            cookies = {"FedAuth": fedauth, "rtFa": rtfa or ""}
            response = requests.get(
                test_url,
                headers=headers,
                cookies=cookies,
                timeout=12,
                allow_redirects=False,
            )

            if response.status_code in (301, 302, 303, 307, 308):
                loc = (response.headers.get("Location") or "").lower()
                if "login" in loc or "authenticate" in loc:
                    return {
                        "success": False,
                        "message": "Cookies expired — SharePoint redirected to Microsoft sign-in.",
                        "status_code": response.status_code,
                    }

            if response.status_code == 200:
                ct = (response.headers.get("Content-Type") or "").lower()
                if "json" in ct:
                    try:
                        data = response.json()
                        user_obj = data.get("d") if isinstance(data.get("d"), dict) else data
                        email = user_obj.get("Email") or user_obj.get("UserPrincipalName") or ""
                        title = user_obj.get("Title") or ""
                        who = f"{title} ({email})" if title and email else (email or title or "authenticated user")
                        return {
                            "success": True,
                            "message": f"Cookies are valid and authenticated as {who}",
                            "user": who,
                            "status_code": 200,
                        }
                    except ValueError:
                        pass
                # Fallback if custom HTML test_url was passed
                low = (response.text or "").lower()
                if "login.microsoftonline.com" not in low and ("signout" in low or "logout" in low):
                    return {
                        "success": True,
                        "message": "Cookies are valid and authenticated",
                        "status_code": 200,
                    }
                return {
                    "success": False,
                    "message": "Cookies may be expired or returned unexpected HTML",
                    "status_code": 200,
                }

            if response.status_code in (401, 403):
                return {
                    "success": False,
                    "message": f"HTTP {response.status_code}: Cookies rejected or insufficient permission.",
                    "status_code": response.status_code,
                }

            return {
                "success": False,
                "message": f"HTTP {response.status_code}: {response.reason}",
                "status_code": response.status_code,
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"Connection test failed: {e}",
                "error": str(e),
            }
