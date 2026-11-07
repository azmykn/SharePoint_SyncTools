"""
Unit tests for SharePoint REST folder listing (path normalization, OData parsing, browse helper).
No network calls — uses mocked session responses.
"""

import json
import os
import sys
import unittest
import unicodedata
import urllib.parse
from types import SimpleNamespace

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from sp_sync.sharepoint.rest_folder_list import (
    encode_path_for_get_folder_param,
    extract_embedded_expand_results,
    fetch_odata_collection_all_pages,
    list_folder_children_rest,
    normalize_server_relative_folder_path,
    _odata_atp_query_value,
    _parse_collection_page,
    _v2_shares_children_api_url,
    _v2_shares_list_children,
    _v2_share_token_from_url,
)


def _resp(status, payload=None, text="", headers=None):
    r = SimpleNamespace(
        status_code=status,
        text=text if text else (json.dumps(payload) if payload is not None else ""),
        headers=headers or {},
    )

    def _json():
        if payload is None:
            raise ValueError("no json")
        return payload

    r.json = _json
    return r


class TestNormalizePath(unittest.TestCase):
    def test_strips_query_and_forces_leading_slash(self):
        raw = "/personal/u/Documents/Lib?foo=1"
        self.assertEqual(
            normalize_server_relative_folder_path(raw),
            "/personal/u/Documents/Lib",
        )

    def test_percent_encoded_arabic_matches_decoded(self):
        arabic = "تسجيلات"
        encoded_seg = "".join("%" + format(b, "02X") for b in arabic.encode("utf-8"))
        a = normalize_server_relative_folder_path(f"/personal/u/Documents/{encoded_seg}")
        b = normalize_server_relative_folder_path(f"/personal/u/Documents/{arabic}")
        self.assertEqual(a, b)

    def test_nfc_equivalence_for_encoding(self):
        # NFC vs NFD for same visual string should normalize to same path bytes for REST
        nfc = unicodedata.normalize("NFC", "caf\u00e9")
        nfd = unicodedata.normalize("NFD", "caf\u00e9")
        self.assertNotEqual(nfc, nfd)
        self.assertEqual(
            encode_path_for_get_folder_param(normalize_server_relative_folder_path(f"/x/{nfd}")),
            encode_path_for_get_folder_param(normalize_server_relative_folder_path(f"/x/{nfc}")),
        )


class TestParseCollectionPage(unittest.TestCase):
    def test_verbose_results_and_next(self):
        items, nxt = _parse_collection_page(
            {"d": {"results": [{"Name": "a"}], "__next": "https://host/_api/continue"}}
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(nxt, "https://host/_api/continue")

    def test_value_array(self):
        items, nxt = _parse_collection_page(
            {"value": [{"id": 1}], "@odata.nextLink": "https://next"}
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(nxt, "https://next")


class TestODataAtpQuery(unittest.TestCase):
    def test_percent_encodes_literal(self):
        q = _odata_atp_query_value("/personal/u/Documents/تسجيلات")
        self.assertTrue(q.startswith("%27") or "%27" in q)

    def test_doubles_single_quote_in_path(self):
        q = _odata_atp_query_value("/x/O'Neil/y")
        decoded = urllib.parse.unquote(q)
        self.assertIn("''", decoded)


class TestListFolderODataAtpFallback(unittest.TestCase):
    class _Sess:
        def __init__(self, responses):
            self._r = list(responses)
            self.calls = []
            self.headers = {}

        def get(self, url, headers=None, timeout=None, **kwargs):
            self.calls.append(url)
            if not self._r:
                return _resp(500, text="no more")
            return self._r.pop(0)

    def test_odata_atp_used_when_classic_folders_403(self):
        expand_payload = {"d": {"Folders": {"results": []}, "Files": {"results": []}}}
        ok_folder = {"d": {"results": [{"Name": "Sub", "ServerRelativeUrl": "/p/Doc/Sub"}]}}
        ok_files = {"d": {"results": []}}
        sess = self._Sess(
            [
                _resp(200, expand_payload),
                _resp(403, text="classic denied"),
                _resp(200, ok_folder),
                _resp(200, ok_files),
            ]
        )
        out = list_folder_children_rest(sess, "https://host/personal/u", "/p/Doc", timeout=5)
        self.assertTrue(out["ok"])
        self.assertEqual(out["method"], "split_odata_atp")
        self.assertEqual(len(out["folders"]), 1)
        self.assertIn("GetFolderByServerRelativeUrl(@p)", sess.calls[2])
        self.assertIn("@p=", sess.calls[2])


class TestListFolderV2SharesFallback(unittest.TestCase):
    class _Sess:
        def __init__(self, responses):
            self._r = list(responses)
            self.calls = []
            self.headers = {}

        def get(self, url, headers=None, timeout=None, **kwargs):
            self.calls.append(url)
            if not self._r:
                return _resp(500, text="no more")
            return self._r.pop(0)

    def test_v2_tried_first_when_sharing_link_present(self):
        share = "https://tenant-my.sharepoint.com/:f:/g/personal/u_token/abc?e=1"
        v2_payload = {
            "value": [
                {"name": "Sub", "folder": {}},
                {"name": "a.txt", "file": {}, "size": 12},
            ]
        }
        sess = self._Sess([_resp(200, v2_payload)])
        out = list_folder_children_rest(
            sess,
            "https://tenant-my.sharepoint.com/personal/u",
            "/personal/u/Documents/T",
            timeout=5,
            sharing_link_url=share,
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["method"], "v2_shares")
        self.assertEqual(len(out["folders"]), 1)
        self.assertEqual(len(out["files"]), 1)
        self.assertEqual(len(sess.calls), 1)
        self.assertIn("/_api/v2.0/shares/", sess.calls[0])
        self.assertTrue(sess.calls[0].startswith("https://tenant-my.sharepoint.com"))

    def test_v2_empty_folder_ok(self):
        share = "https://tenant-my.sharepoint.com/:f:/g/personal/u/abc?e=1"
        sess = self._Sess([_resp(200, {"value": []})])
        out = list_folder_children_rest(
            sess,
            "https://tenant-my.sharepoint.com/personal/u",
            "/personal/u/Documents/T",
            timeout=5,
            sharing_link_url=share,
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["method"], "v2_shares")
        self.assertEqual(out["folders"], [])
        self.assertEqual(out["files"], [])

    def test_v2_403_short_circuits_without_classic(self):
        share = "https://tenant-my.sharepoint.com/:f:/g/personal/u/abc?e=1"
        sess = self._Sess([_resp(403, text='{"error":{}}')])
        out = list_folder_children_rest(
            sess,
            "https://tenant-my.sharepoint.com/personal/u",
            "/personal/u/Documents/T",
            timeout=5,
            sharing_link_url=share,
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["method"], "v2_shares")
        self.assertEqual(out.get("upstream_http_status"), 403)
        self.assertEqual(len(sess.calls), 1)


class TestExpandExtract(unittest.TestCase):
    def test_expand_shape(self):
        payload = {
            "d": {
                "Folders": {
                    "results": [
                        {"Name": "Sub", "ServerRelativeUrl": "/personal/u/Documents/Parent/Sub"}
                    ]
                },
                "Files": {
                    "results": [
                        {
                            "Name": "f.txt",
                            "ServerRelativeUrl": "/personal/u/Documents/Parent/f.txt",
                            "Length": 10,
                        }
                    ]
                },
            }
        }
        f = extract_embedded_expand_results(payload, "Folders")
        fi = extract_embedded_expand_results(payload, "Files")
        self.assertEqual(len(f), 1)
        self.assertEqual(len(fi), 1)


class TestFetchPagination(unittest.TestCase):
    class _Sess:
        def __init__(self, pages):
            self._pages = pages
            self.calls = []

        def get(self, url, headers=None, timeout=None, **kwargs):
            self.calls.append(url)
            return self._pages.pop(0)

    def test_follows_d_next(self):
        sess = self._Sess(
            [
                _resp(
                    200,
                    {
                        "d": {
                            "results": [{"Name": "a", "ServerRelativeUrl": "/a"}],
                            "__next": "https://tenant/page2",
                        }
                    },
                ),
                _resp(
                    200,
                    {"d": {"results": [{"Name": "b", "ServerRelativeUrl": "/b"}]}},
                ),
            ]
        )
        items, st, err = fetch_odata_collection_all_pages(
            sess, "https://tenant/_api/start", {"Accept": "application/json;odata=verbose"}, timeout=5
        )
        self.assertEqual(st, 200)
        self.assertEqual(err, "")
        self.assertEqual(len(items), 2)
        self.assertEqual(len(sess.calls), 2)


class TestListFolderChildrenRest(unittest.TestCase):
    class _Sess:
        def __init__(self, responses):
            self._r = list(responses)
            self.calls = []
            self.headers = {}

        def get(self, url, headers=None, timeout=None, **kwargs):
            self.calls.append(url)
            if not self._r:
                return _resp(500, text="no more")
            return self._r.pop(0)

    def test_expand_empty_arrays_fallback_to_split(self):
        """SPO may return $expand with empty Folders/Files while /Folders lists children."""
        expand_payload = {"d": {"Folders": {"results": []}, "Files": {"results": []}}}
        sess = self._Sess(
            [
                _resp(200, expand_payload),
                _resp(
                    200,
                    {"d": {"results": [{"Name": "Sub", "ServerRelativeUrl": "/w/Sub"}]}},
                ),
                _resp(200, {"d": {"results": []}}),
            ]
        )
        out = list_folder_children_rest(sess, "https://host/sites/Site", "/w", timeout=5)
        self.assertTrue(out["ok"])
        self.assertEqual(out["method"], "split")
        self.assertEqual(len(out["folders"]), 1)
        self.assertEqual(out["folders"][0]["name"], "Sub")

    def test_expand_success_short_circuit(self):
        expand_payload = {
            "d": {
                "Folders": {"results": [{"Name": "F1", "ServerRelativeUrl": "/w/F1"}]},
                "Files": {"results": []},
            }
        }
        sess = self._Sess([_resp(200, expand_payload)])
        out = list_folder_children_rest(sess, "https://host/sites/Site", "/w", timeout=5)
        self.assertTrue(out["ok"])
        self.assertEqual(out["method"], "expand")
        self.assertEqual(len(out["folders"]), 1)
        self.assertEqual(out["folders"][0]["name"], "F1")

    def test_fallback_split_when_expand_empty_d(self):
        sess = self._Sess(
            [
                _resp(200, {"d": {}}),
                _resp(
                    200,
                    {"d": {"results": [{"Name": "X", "ServerRelativeUrl": "/w/X"}]}},
                ),
                _resp(200, {"d": {"results": []}}),
            ]
        )
        out = list_folder_children_rest(sess, "https://host/sites/Site", "/w", timeout=5)
        self.assertTrue(out["ok"])
        self.assertEqual(out["method"], "split")
        self.assertEqual(len(out["folders"]), 1)

    def test_browse_empty_hint_nonempty(self):
        from sp_sync.sharepoint.rest_folder_list import browse_empty_hint_i18n

        hi = browse_empty_hint_i18n()
        self.assertIn("FedAuth", hi["en"])
        self.assertTrue(len(hi.get("ar", "")) > 20)


class TestWebRelativePath(unittest.TestCase):
    def test_suffix_under_personal_web(self):
        from sp_sync.sharepoint.rest_folder_list import _web_relative_folder_path

        self.assertEqual(
            _web_relative_folder_path(
                "https://tenant-my.sharepoint.com/personal/user_upn",
                "/personal/user_upn/Documents/تسجيلات",
            ),
            "/Documents/تسجيلات",
        )
        self.assertIsNone(
            _web_relative_folder_path("https://tenant-my.sharepoint.com", "/personal/u/Documents/X")
        )


class TestListFolderWebrelRetry(unittest.TestCase):
    class _Sess:
        def __init__(self, responses):
            self._r = list(responses)
            self.calls = []
            self.headers = {}

        def get(self, url, headers=None, timeout=None, **kwargs):
            self.calls.append(url)
            if not self._r:
                return _resp(500, text="no more")
            return self._r.pop(0)

    def test_second_try_webrel_populates(self):
        empty_expand = {"d": {}}
        empty_coll = {"d": {"results": []}}
        web_folder = {
            "d": {
                "results": [
                    {"Name": "Inner", "ServerRelativeUrl": "/personal/u/Documents/F/Inner"},
                ]
            }
        }
        sess = self._Sess(
            [
                _resp(200, empty_expand),
                _resp(200, empty_coll),
                _resp(200, empty_coll),
                _resp(200, web_folder),
                _resp(200, empty_coll),
            ]
        )
        base = "https://host/personal/user_upn"
        norm = "/personal/user_upn/Documents/F"
        out = list_folder_children_rest(sess, base, norm, timeout=5)
        self.assertTrue(out["ok"])
        self.assertEqual(out["method"], "split_webrel")
        self.assertEqual(len(out["folders"]), 1)
        self.assertEqual(out["folders"][0]["name"], "Inner")


class TestV2SharesChildrenUrl(unittest.TestCase):
    def test_share_root_children(self):
        url = _v2_shares_children_api_url(
            "https://tenant-my.sharepoint.com",
            "u!abc",
            "/personal/u/Documents/Root",
            share_root_norm="/personal/u/Documents/Root",
        )
        self.assertIn("/driveItem/children", url)
        self.assertNotIn(":/", url.split("driveItem")[-1])

    def test_subfolder_by_relative_path(self):
        root = "/personal/u/Documents/Parent"
        sub = root + "/Sub"
        url = _v2_shares_children_api_url(
            "https://tenant-my.sharepoint.com",
            "u!abc",
            sub,
            share_root_norm=root,
        )
        self.assertIn("driveItem:/Sub:/children", url)

    def test_subfolder_by_item_id(self):
        url = _v2_shares_children_api_url(
            "https://tenant-my.sharepoint.com",
            "u!abc",
            "/any",
            parent_item_id="01ITEMID",
        )
        self.assertIn("/driveItem/01ITEMID/children", url)


class TestV2SharesPagination(unittest.TestCase):
    class _Sess:
        def __init__(self, pages):
            self._pages = pages
            self.headers = {}
            self.calls = []

        def get(self, url, headers=None, timeout=None, **kwargs):
            self.calls.append(url)
            return self._pages.pop(0)

    def test_v2_shares_follows_next_link(self):
        page1 = {
            "value": [
                {"name": "Folder1", "folder": {}, "id": "id1"},
                {"name": "file1.txt", "size": 100, "id": "id2"},
            ],
            "@odata.nextLink": "https://tenant-my.sharepoint.com/_api/v2.0/shares/u!abc/driveItem/children?$skiptoken=foo",
        }
        page2 = {
            "value": [
                {"name": "file2.pdf", "size": 200, "id": "id3"},
            ]
        }
        sess = self._Sess([_resp(200, page1), _resp(200, page2)])
        out = _v2_shares_list_children(
            sess,
            "https://tenant-my.sharepoint.com",
            "https://tenant-my.sharepoint.com/:f:/g/personal/token",
            "/personal/u/Documents/Root",
            timeout=5,
        )
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["folders"]), 1)
        self.assertEqual(len(out["files"]), 2)
        self.assertEqual(len(sess.calls), 2)


if __name__ == "__main__":
    unittest.main()
