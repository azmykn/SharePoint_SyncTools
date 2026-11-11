"""Download ledger helpers: backfill existing local files and decide per remote file.

Rule (owner decision): an item recorded in the ledger is never downloaded again,
even if the local copy was renamed, moved, trimmed or deleted.  The only way to
get it again is an explicit re-download request from the UI / API.
"""

from __future__ import annotations

import os
from collections import defaultdict
from typing import Callable, Iterable, Optional

from sp_sync.db.store import ledger_key
from sp_sync.sharepoint.naming import (
    is_video_name,
    lecture_search_dirs,
    recordings_dir,
    lecture_datetime,
    parse_lecture_filename,
    preferred_download_path,
    resolve_existing_lecture_path,
)

LogFn = Callable[[str], None]


def _noop(_msg: str) -> None:
    pass


def _row_key(row: dict) -> str:
    return ledger_key(row.get("item_id"), row.get("rel_url"))


def _local_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return -1


def resolve_ledger_path(local_path: Optional[str]) -> Optional[str]:
    """Existing file for a ledger path: as recorded, else same name in ``تسجيلات`` or the old top level."""
    if not local_path:
        return None
    if os.path.isfile(local_path):
        return local_path
    d, name = os.path.split(os.path.abspath(local_path))
    for cand_dir in lecture_search_dirs(d):
        cand = os.path.join(cand_dir, name)
        if os.path.isfile(cand):
            return cand
    return None


def ledger_names_for(store, download_dir: str) -> list:
    names = []
    for d in lecture_search_dirs(download_dir):
        names.extend(store.ledger_names_in_dir(d))
    return names


def _record(store, row: dict, local_path: str, source: str) -> None:
    store.ledger_record(
        item_id=row.get("item_id"),
        rel_url=row.get("rel_url"),
        original_name=row.get("name") or "",
        size=row.get("size") or None,
        modified=row.get("modified"),
        local_path=local_path,
        source=source,
    )


def backfill_folder(store, download_dir: str, files: Iterable[dict], log: Optional[LogFn] = None) -> int:
    """Record remote files that already exist locally (under any known name) without downloading.

    Matching order for each remote file not yet in the ledger:
      1. old names: the original long Teams name, or the v1 short ``CODE_date_HHMM`` name;
      2. v2 names ``<subject>_<date>_<NN>``: same recording date; when several remote
         files share a date, exact size wins first, then chronological order <-> NN order,
         but only if the counts match (otherwise ambiguous -> left for download, logged).
    Returns the number of rows recorded.
    """
    log = log or _noop
    folder_hint = os.path.basename(os.path.abspath(download_dir.rstrip("/\\"))) or ""
    pending = [
        r for r in (files or [])
        if (r.get("name") and _row_key(r) and store.ledger_get(_row_key(r)) is None)
    ]
    if not pending:
        return 0
    claimed = set(store.ledger_paths())
    recorded = 0

    # 1) old names
    rest = []
    for r in pending:
        p = resolve_existing_lecture_path(
            download_dir, r["name"], folder_hint=folder_hint, modified=r.get("modified")
        )
        if p and os.path.abspath(p) not in claimed:
            _record(store, r, p, "backfill")
            claimed.add(os.path.abspath(p))
            recorded += 1
        else:
            rest.append(r)

    # 2) v2 names grouped by date
    local_by_date: dict[str, list[tuple[int, str]]] = defaultdict(list)
    seen_names = set()
    for d in lecture_search_dirs(download_dir):
        try:
            local_names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in local_names:
            parsed = parse_lecture_filename(n)
            path = os.path.abspath(os.path.join(d, n))
            if parsed and n not in seen_names and os.path.isfile(path) and path not in claimed:
                seen_names.add(n)
                local_by_date[parsed["date"]].append((parsed["seq"], path))

    remote_by_date: dict[str, list] = defaultdict(list)
    for r in rest:
        if not is_video_name(r["name"]):
            continue
        when = lecture_datetime(r["name"], r.get("modified"))
        if when is None:
            continue
        remote_by_date[when.strftime("%Y-%m-%d")].append((when, r))

    for date, remotes in remote_by_date.items():
        locals_ = sorted(local_by_date.get(date, []))
        if not locals_:
            continue
        remotes = sorted(remotes, key=lambda t: t[0])
        # exact size match first
        for when, r in list(remotes):
            size = int(r.get("size") or 0)
            if size <= 0:
                continue
            hit = next((lp for lp in locals_ if _local_size(lp[1]) == size), None)
            if hit:
                _record(store, r, hit[1], "backfill")
                locals_.remove(hit)
                remotes.remove((when, r))
                recorded += 1
        if not remotes or not locals_:
            continue
        if len(remotes) == len(locals_):
            for (when, r), (_seq, path) in zip(remotes, locals_):
                _record(store, r, path, "backfill")
                recorded += 1
        else:
            log(
                f"  ⚠️ Ledger backfill: {len(remotes)} remote vs {len(locals_)} local lecture(s) on "
                f"{date} - ambiguous, not matched"
            )
    if recorded:
        log(f"  📒 Ledger backfill: recorded {recorded} already-downloaded file(s)")
    return recorded


def plan_download(store, download_dir: str, row: dict, log: Optional[LogFn] = None) -> Optional[str]:
    """Return the local path to download *row* to, or ``None`` to skip it (in ledger)."""
    log = log or _noop
    name = row.get("name") or ""
    entry = store.ledger_get(_row_key(row))
    if entry and not entry.get("redownload_requested"):
        shown = os.path.basename(entry.get("local_path") or "") or name
        log(f"[-] Skip '{name}' (in download ledger as '{shown}')")
        return None
    if entry and entry.get("redownload_requested"):
        recorded = entry.get("local_path") or ""
        target = resolve_ledger_path(recorded)
        if not target and recorded and is_video_name(recorded):
            target = os.path.join(recordings_dir(download_dir), os.path.basename(recorded))
        elif not target and recorded and os.path.isdir(os.path.dirname(recorded)):
            target = recorded
        if not target:
            target = preferred_download_path(
                download_dir, name, modified=row.get("modified"),
                extra_names=ledger_names_for(store, download_dir),
            )
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.exists(target):
            stem, ext = os.path.splitext(target)
            aside = f"{stem}_old{ext}"
            i = 2
            while os.path.exists(aside):
                aside = f"{stem}_old{i}{ext}"
                i += 1
            os.rename(target, aside)
            log(f"  ↪ Re-download requested: kept current copy as '{os.path.basename(aside)}'")
        return target
    folder_hint = os.path.basename(os.path.abspath(download_dir.rstrip("/\\"))) or ""
    target = preferred_download_path(
        download_dir,
        name,
        folder_hint=folder_hint,
        modified=row.get("modified"),
        extra_names=ledger_names_for(store, download_dir),
    )
    os.makedirs(os.path.dirname(target), exist_ok=True)
    return target


def record_after_download(
    store, row: dict, local_path: str, downloaded: int, existed_before: bool,
    is_valid: Callable[[str], bool] = lambda p: True,
) -> bool:
    """Record a row after ``_download_one_file``; returns True if recorded."""
    if not _row_key(row) or not os.path.isfile(local_path):
        return False
    if downloaded:
        _record(store, row, local_path, "download")
        return True
    if existed_before and is_valid(local_path):
        _record(store, row, local_path, "existing")
        return True
    return False
