"""SharePoint cookie validation (no external files)."""


class CookieValidator:
    """Validates SharePoint cookies for freshness and authenticity."""

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
            result["recommendations"].append("Cookies appear valid. Test with a sync operation.")

        return result

    def test_cookie_connectivity(self, fedauth, rtfa, test_url=None):
        if test_url is None:
            test_url = (
                "https://comfac001-my.sharepoint.com/personal/"
                "waleedelsaeed_comfac001_onmicrosoft_com/_layouts/15/MySite.aspx"
            )

        try:
            import requests

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "gzip, deflate",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            }

            cookies = {"FedAuth": fedauth, "rtFa": rtfa}

            response = requests.get(test_url, headers=headers, cookies=cookies, timeout=10)

            if response.status_code == 200:
                if "signout" in response.text.lower() or "logout" in response.text.lower():
                    return {
                        "success": True,
                        "message": "Cookies are valid and authenticated",
                        "status_code": response.status_code,
                    }
                return {
                    "success": False,
                    "message": "Cookies may be expired or invalid",
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
                "message": f"Connection test failed: {str(e)}",
                "error": str(e),
            }
