"""Short lecture filenames derived from SharePoint / Teams metadata."""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Optional, Tuple

# e.g. MBA-09, EMBA-12, CS101 (letter+digits with optional dash)
_COURSE_CODE_RE = re.compile(r"\b([A-Za-z]{2,10}-?\d{1,4})\b")

# Common Teams / Stream / SharePoint recording stamps in the leaf name
_DT_PATTERNS = [
    re.compile(
        r"(?P<y>20\d{2})[-_/](?P<m>\d{2})[-_/](?P<d>\d{2})[ T_-]"
        r"(?P<H>\d{2})[:\-.]?(?P<M>\d{2})(?:[:\-.]?(?P<S>\d{2}))?"
    ),
    re.compile(
        r"(?P<y>20\d{2})(?P<m>\d{2})(?P<d>\d{2})[_-]?"
        r"(?P<H>\d{2})(?P<M>\d{2})(?:(?P<S>\d{2}))?"
    ),
]


def extract_course_code(*texts: Optional[str]) -> Optional[str]:
    for text in texts:
        if not text:
            continue
        m = _COURSE_CODE_RE.search(str(text))
        if m:
            return m.group(1).upper()
    return None


def extract_datetime_from_name(name: str) -> Optional[datetime]:
    stem = os.path.splitext(name or "")[0]
    for pat in _DT_PATTERNS:
        m = pat.search(stem)
        if not m:
            continue
        try:
            return datetime(
                int(m.group("y")),
                int(m.group("m")),
                int(m.group("d")),
                int(m.group("H")),
                int(m.group("M")),
                int(m.group("S") or 0),
            )
        except (ValueError, TypeError):
            continue
    return None


def parse_sharepoint_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    # SharePoint often returns ISO-8601 with Z
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw[:19], fmt)
        except ValueError:
            continue
    return None


def build_short_lecture_filename(
    original_name: str,
    *,
    folder_hint: Optional[str] = None,
    modified: Optional[str] = None,
) -> str:
    """Build ``MBA-09_2026-11-07_1649.mp4`` from the original leaf name / folder / time."""
    original_name = os.path.basename(original_name or "") or "lecture.mp4"
    stem, ext = os.path.splitext(original_name)
    if not ext:
        ext = ".mp4"
    code = extract_course_code(folder_hint, stem) or "LECTURE"
    dt = extract_datetime_from_name(original_name) or parse_sharepoint_time(modified)
    if dt is None:
        # Stable fallback so existence checks stay deterministic without inventing a clock time.
        stamp = "unknown"
    else:
        stamp = dt.strftime("%Y-%m-%d_%H%M")
    safe_code = re.sub(r"[^\w\-]+", "_", code).strip("_") or "LECTURE"
    return f"{safe_code}_{stamp}{ext.lower()}"


def lecture_candidate_names(
    original_name: str,
    *,
    folder_hint: Optional[str] = None,
    modified: Optional[str] = None,
) -> Tuple[str, str]:
    """Return ``(short_name, original_basename)``."""
    original_basename = os.path.basename(original_name or "") or "lecture.mp4"
    short = build_short_lecture_filename(
        original_basename, folder_hint=folder_hint, modified=modified
    )
    return short, original_basename


def resolve_existing_lecture_path(
    download_dir: str,
    original_name: str,
    *,
    folder_hint: Optional[str] = None,
    modified: Optional[str] = None,
) -> Optional[str]:
    """Return path if either the short or long filename already exists (file, not .part)."""
    short, original = lecture_candidate_names(
        original_name, folder_hint=folder_hint, modified=modified
    )
    for name in (short, original):
        path = os.path.join(download_dir, name)
        if os.path.isfile(path):
            return path
    return None


def preferred_download_path(
    download_dir: str,
    original_name: str,
    *,
    folder_hint: Optional[str] = None,
    modified: Optional[str] = None,
) -> str:
    """Path to write a new download (short name), unless the long name already exists."""
    existing = resolve_existing_lecture_path(
        download_dir, original_name, folder_hint=folder_hint, modified=modified
    )
    if existing:
        return existing
    short, _original = lecture_candidate_names(
        original_name, folder_hint=folder_hint, modified=modified
    )
    return os.path.join(download_dir, short)
