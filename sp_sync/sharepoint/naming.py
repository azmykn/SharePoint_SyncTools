"""Lecture filenames derived from SharePoint / Teams metadata.

Current format (v2): ``<subject>_<YYYY-MM-DD>_<NN>.mp4`` where *subject* is the
local folder name without its trailing program code (``MBA-09``) and *NN* is a
per-folder sequence in chronological order.  Older names (the original long
Teams name and the v1 short ``MBA-09_2026-11-07_1649.mp4``) are still
recognised so nothing is downloaded twice.
"""

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


VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".m4v", ".webm", ".avi"}

_TRAILING_CODE_RE = re.compile(r"[\s\-_]*\b[A-Za-z]{2,10}-?\d{1,4}\s*$")
_NEW_NAME_RE = re.compile(
    r"^(?P<subject>.+)_(?P<date>20\d{2}-\d{2}-\d{2}|unknown)_(?P<seq>\d{2,3})(?P<ext>\.[A-Za-z0-9]+)$"
)
_UNSAFE_CHARS_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def is_video_name(name: str) -> bool:
    return os.path.splitext(name or "")[1].lower() in VIDEO_EXTS


def subject_from_folder(folder: Optional[str]) -> str:
    """``'إدارة التحول الرقمي MBA-09'`` -> ``'إدارة التحول الرقمي'``."""
    base = os.path.basename((folder or "").rstrip("/\\")).strip()
    subject = _TRAILING_CODE_RE.sub("", base).strip(" -_")
    subject = _UNSAFE_CHARS_RE.sub("_", subject).strip()
    return subject or _UNSAFE_CHARS_RE.sub("_", base).strip() or "lecture"


def parse_lecture_filename(name: str) -> Optional[dict]:
    """Parse a v2 name; returns ``{subject, date, seq, ext}`` or ``None``."""
    m = _NEW_NAME_RE.match(os.path.basename(name or ""))
    if not m:
        return None
    return {
        "subject": m.group("subject"),
        "date": m.group("date"),
        "seq": int(m.group("seq")),
        "ext": m.group("ext").lower(),
    }


def lecture_datetime(original_name: str, modified: Optional[str] = None) -> Optional[datetime]:
    """Recording time from the long Teams name, else SharePoint modified time."""
    return extract_datetime_from_name(os.path.basename(original_name or "")) or parse_sharepoint_time(modified)


def build_lecture_filename(subject: str, when: Optional[datetime], seq: int, ext: str = ".mp4") -> str:
    date = when.strftime("%Y-%m-%d") if when else "unknown"
    ext = (ext or ".mp4").lower()
    if not ext.startswith("."):
        ext = "." + ext
    return f"{subject}_{date}_{int(seq):02d}{ext}"


def next_lecture_sequence(download_dir: str, extra_names=()) -> int:
    """Next NN for a folder: max(seq of v2 names on disk + ledger names) + 1."""
    names = list(extra_names or ())
    try:
        names.extend(os.listdir(download_dir))
    except OSError:
        pass
    best = 0
    for n in names:
        parsed = parse_lecture_filename(n)
        if parsed:
            best = max(best, parsed["seq"])
    return best + 1


def new_lecture_download_path(
    download_dir: str,
    original_name: str,
    *,
    modified: Optional[str] = None,
    extra_names=(),
) -> str:
    """Fresh v2 path for a lecture that was never downloaded."""
    ext = os.path.splitext(original_name or "")[1] or ".mp4"
    subject = subject_from_folder(download_dir)
    when = lecture_datetime(original_name, modified)
    seq = next_lecture_sequence(download_dir, extra_names)
    while True:
        path = os.path.join(download_dir, build_lecture_filename(subject, when, seq, ext))
        if not os.path.exists(path) and not os.path.exists(path + ".part"):
            return path
        seq += 1


def preferred_download_path(
    download_dir: str,
    original_name: str,
    *,
    folder_hint: Optional[str] = None,
    modified: Optional[str] = None,
    extra_names=(),
) -> str:
    """Existing old long/short path if present, else a new v2 name (videos) or the original name."""
    existing = resolve_existing_lecture_path(
        download_dir, original_name, folder_hint=folder_hint, modified=modified
    )
    if existing:
        return existing
    original = os.path.basename(original_name or "") or "lecture.mp4"
    if not is_video_name(original):
        return os.path.join(download_dir, original)
    return new_lecture_download_path(
        download_dir, original, modified=modified, extra_names=extra_names
    )
