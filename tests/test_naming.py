"""Short lecture filenames and dual existence checks."""

import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sp_sync.sharepoint.naming import (
    build_short_lecture_filename,
    extract_course_code,
    extract_datetime_from_name,
    preferred_download_path,
    resolve_existing_lecture_path,
)


class TestNaming(unittest.TestCase):
    def test_extract_course_code(self):
        self.assertEqual(extract_course_code("MBA-09 Recordings"), "MBA-09")
        self.assertEqual(
            extract_course_code(
                "MBA-09 MBA-09 Recording-202611071649.mp4", "other"
            ),
            "MBA-09",
        )

    def test_extract_datetime_from_name(self):
        dt = extract_datetime_from_name("MBA-09 Recording 2026-11-07_16-49-00.mp4")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 11)
        self.assertEqual(dt.day, 7)
        self.assertEqual(dt.hour, 16)
        self.assertEqual(dt.minute, 49)

        dt2 = extract_datetime_from_name("Lecture-20261107164912.mp4")
        self.assertIsNotNone(dt2)
        self.assertEqual(dt2.strftime("%Y-%m-%d %H%M"), "2026-11-07 1649")

    def test_build_short_name_example(self):
        short = build_short_lecture_filename(
            "MBA-09 MBA-09 Recording - 2026-11-07_16-49-22 - something long.mp4",
            folder_hint="MBA-09",
        )
        self.assertEqual(short, "MBA-09_2026-11-07_1649.mp4")

    def test_build_short_name_from_modified_fallback(self):
        short = build_short_lecture_filename(
            "Weekly Sync.mp4",
            folder_hint="EMBA-01",
            modified="2026-03-15T09:05:00Z",
        )
        self.assertEqual(short, "EMBA-01_2026-03-15_0905.mp4")

    def test_existence_recognizes_old_and_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            long_name = "MBA-09 MBA-09 Recording - 2026-11-07_16-49-22 - long.mp4"
            short = build_short_lecture_filename(long_name, folder_hint="MBA-09")
            # Old long name present → resolve to it and do not re-download path
            long_path = os.path.join(tmp, long_name)
            with open(long_path, "wb") as f:
                f.write(b"x")
            found = resolve_existing_lecture_path(tmp, long_name, folder_hint="MBA-09")
            self.assertEqual(found, long_path)
            self.assertEqual(
                preferred_download_path(tmp, long_name, folder_hint="MBA-09"), long_path
            )

            os.remove(long_path)
            short_path = os.path.join(tmp, short)
            with open(short_path, "wb") as f:
                f.write(b"x")
            found2 = resolve_existing_lecture_path(tmp, long_name, folder_hint="MBA-09")
            self.assertEqual(found2, short_path)

            os.remove(short_path)
            preferred = preferred_download_path(tmp, long_name, folder_hint="MBA-09")
            self.assertEqual(preferred, os.path.join(tmp, short))


if __name__ == "__main__":
    unittest.main()
