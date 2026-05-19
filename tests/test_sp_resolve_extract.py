"""Unit tests for SharePoint URL path extraction helpers (no network)."""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from sp_sync.sharepoint.sync_engine import (
    _sp_extract_onedrive_id_param,
    _sp_extract_server_rel_from_sharepoint_html,
)


class TestExtractOnedriveIdParam(unittest.TestCase):
    def test_case_insensitive_id(self):
        u = "https://tenant-my.sharepoint.com/personal/u/_layouts/15/onedrive.aspx?ID=%2Fpersonal%2Fu%2FDocuments%2FFoo"
        self.assertEqual(
            _sp_extract_onedrive_id_param(u),
            "/personal/u/Documents/Foo",
        )

    def test_fragment_encoded_equals(self):
        u = "https://tenant-my.sharepoint.com/foo#id%3D%2Fpersonal%2Fu%2FDocuments%2FBar"
        self.assertEqual(
            _sp_extract_onedrive_id_param(u),
            "/personal/u/Documents/Bar",
        )

    def test_rootfolder_alias(self):
        u = "https://x/personal/u/_layouts/15/start.aspx?rootfolder=%2Fpersonal%2Fu%2FDocuments"
        out = _sp_extract_onedrive_id_param(u)
        self.assertTrue(out.endswith("/personal/u/Documents"))


class TestSharingLinkRemovedHtml(unittest.TestCase):
    def test_en_removed(self):
        from sp_sync.sharepoint.sync_engine import _sp_body_indicates_sharing_link_removed

        html = "<html><h1>This link has been removed</h1><p>access to this document has been removed</p></html>"
        self.assertTrue(_sp_body_indicates_sharing_link_removed(html))

    def test_ok_page(self):
        from sp_sync.sharepoint.sync_engine import _sp_body_indicates_sharing_link_removed

        self.assertFalse(_sp_body_indicates_sharing_link_removed("<html>onedrive.aspx?id=/personal/x</html>"))


class TestExtractFromHtml(unittest.TestCase):
    def test_server_relative_url_json(self):
        html = 'blah "serverRelativeUrl":"/personal/u_upn/Documents/تسجيلات" blah'
        self.assertEqual(
            _sp_extract_server_rel_from_sharepoint_html(html),
            "/personal/u_upn/Documents/تسجيلات",
        )

    def test_og_url_with_id(self):
        html = '<meta property="og:url" content="https://host/personal/u/_layouts/15/onedrive.aspx?id=%2Fpersonal%2Fu%2FDocuments%2Fx" />'
        self.assertEqual(
            _sp_extract_server_rel_from_sharepoint_html(html),
            "/personal/u/Documents/x",
        )

    def test_list_url_full_https(self):
        html = '"listUrl":"https://host-my.sharepoint.com/personal/u_upn/Documents/MyLib"'
        self.assertEqual(
            _sp_extract_server_rel_from_sharepoint_html(html),
            "/personal/u_upn/Documents/MyLib",
        )


if __name__ == "__main__":
    unittest.main()
