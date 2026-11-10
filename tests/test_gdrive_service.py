"""
Google Drive Service Unit Tests
===============================
"""

import unittest
from sp_sync.gdrive.service import (
    extract_folder_id,
    GOOGLE_DOCS_EXPORT,
    FOLDER_MIME,
)


class TestGDriveService(unittest.TestCase):
    def test_extract_folder_id_from_full_url(self):
        url = "https://drive.google.com/drive/folders/1LIQPMkF2vRlRoe9AAYvlisw8e-a1IX_a"
        self.assertEqual(extract_folder_id(url), "1LIQPMkF2vRlRoe9AAYvlisw8e-a1IX_a")

    def test_extract_folder_id_from_open_id_url(self):
        url = "https://drive.google.com/open?id=1LIQPMkF2vRlRoe9AAYvlisw8e-a1IX_a"
        self.assertEqual(extract_folder_id(url), "1LIQPMkF2vRlRoe9AAYvlisw8e-a1IX_a")

    def test_extract_folder_id_raw_id(self):
        folder_id = "1LIQPMkF2vRlRoe9AAYvlisw8e-a1IX_a"
        self.assertEqual(extract_folder_id(folder_id), folder_id)

    def test_extract_folder_id_mobile_and_file_urls(self):
        self.assertEqual(
            extract_folder_id("https://drive.google.com/drive/u/2/folders/1LIQPMkF2vRlRoe9AAYvlisw8e-a1IX_a?usp=sharing"),
            "1LIQPMkF2vRlRoe9AAYvlisw8e-a1IX_a"
        )
        self.assertEqual(
            extract_folder_id("https://drive.google.com/file/d/1LIQPMkF2vRlRoe9AAYvlisw8e-a1IX_a/view?usp=drivesdk"),
            "1LIQPMkF2vRlRoe9AAYvlisw8e-a1IX_a"
        )
        self.assertEqual(
            extract_folder_id("https://drive.google.com/drive/mobile/folders/1LIQPMkF2vRlRoe9AAYvlisw8e-a1IX_a"),
            "1LIQPMkF2vRlRoe9AAYvlisw8e-a1IX_a"
        )

    def test_extract_folder_id_invalid(self):
        self.assertIsNone(extract_folder_id("https://example.com/not_drive"))
        self.assertIsNone(extract_folder_id(""))
        self.assertIsNone(extract_folder_id("short"))

    def test_has_credentials(self):
        import os
        import tempfile
        from sp_sync.gdrive.service import has_credentials

        self.assertFalse(has_credentials(credentials_file=os.path.join(tempfile.gettempdir(), "no_such_creds.json")))
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write('{"installed": {"client_id": "x"}}')
            path = f.name
        try:
            self.assertTrue(has_credentials(credentials_file=path))
        finally:
            os.unlink(path)

    def test_oauth_status_structure(self):
        from sp_sync.gdrive.service import get_oauth_status
        st = get_oauth_status()
        self.assertIn("authenticated", st)
        self.assertIn("flow_status", st)
        self.assertIn("has_credentials", st)
        self.assertIsInstance(st["has_credentials"], bool)

    def test_google_docs_export_mappings(self):
        self.assertIn("application/vnd.google-apps.document", GOOGLE_DOCS_EXPORT)
        self.assertEqual(GOOGLE_DOCS_EXPORT["application/vnd.google-apps.document"][1], ".pdf")
        self.assertEqual(GOOGLE_DOCS_EXPORT["application/vnd.google-apps.spreadsheet"][1], ".xlsx")
        self.assertEqual(GOOGLE_DOCS_EXPORT["application/vnd.google-apps.presentation"][1], ".pptx")


if __name__ == "__main__":
    unittest.main()
