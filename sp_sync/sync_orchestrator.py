"""Unified Sync Orchestrator — eliminates duplicated sync loops, TOCTOU races, and adds cancellation/dry-run."""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from sp_sync.sync_metrics import set_sync_active, set_sync_status_message, set_sync_stopping


class SyncOrchestrator:
    """Thread-safe coordinator for SharePoint and Google Drive sync jobs."""

    def __init__(
        self,
        *,
        log_fn: Callable[[str, str], None],
        save_report_fn: Callable[[str, dict], None],
    ) -> None:
        self._log = log_fn
        self._save_report = save_report_fn
        self._lock = threading.RLock()
        self.status: Dict[str, Dict[str, Any]] = {
            "sharepoint": {"running": False, "stopping": False, "last_run": None, "last_result": ""},
            "gdrive": {"running": False, "stopping": False, "last_run": None, "last_result": ""},
        }
        self._cancel_events: Dict[str, threading.Event] = {
            "sharepoint": threading.Event(),
            "gdrive": threading.Event(),
        }
        self._threads: Dict[str, Optional[threading.Thread]] = {
            "sharepoint": None,
            "gdrive": None,
        }

    def is_running(self, service: str) -> bool:
        with self._lock:
            return bool(self.status.get(service, {}).get("running"))

    def request_stop(self, service: str = "all") -> Dict[str, Any]:
        """Request graceful cancellation for one service or both."""
        svc = (service or "all").strip().lower()
        targets = ("sharepoint", "gdrive") if svc == "all" else (svc,)
        stopped: List[str] = []
        with self._lock:
            for t in targets:
                if t in self.status and self.status[t]["running"]:
                    self.status[t]["stopping"] = True
                    self._cancel_events[t].set()
                    set_sync_stopping(t)
                    self._log(t, "🛑 Stop requested — finishing current step safely...")
                    stopped.append(t)
        return {"success": True, "stopping": stopped, "service": svc}

    def start_sharepoint(
        self,
        *,
        fedauth: str,
        rtfa: str,
        site_url: str,
        configs: List[dict],
        trigger: str = "manual",
        dry_run: bool = False,
        config_index: Optional[int] = None,
        on_complete: Optional[Callable[[dict], None]] = None,
    ) -> Tuple[bool, str, Optional[threading.Thread]]:
        """Atomically reserve and start a SharePoint sync background thread."""
        with self._lock:
            if self.status["sharepoint"]["running"]:
                return False, "A SharePoint sync is already running.", None
            self.status["sharepoint"]["running"] = True
            self.status["sharepoint"]["stopping"] = False
            cancel_ev = threading.Event()
            self._cancel_events["sharepoint"] = cancel_ev

        if config_index is not None:
            if 0 <= config_index < len(configs):
                active_selected = [(config_index, configs[config_index])]
                paused_selected: List[Tuple[int, dict]] = []
            else:
                with self._lock:
                    self.status["sharepoint"]["running"] = False
                return False, "Invalid folder index.", None
        else:
            # Pre-filter paused/excluded folders UP FRONT before starting the sync loop
            active_selected = [
                (i, c) for i, c in enumerate(configs) if c.get("sync_enabled", True) is not False
            ]
            paused_selected = [
                (i, c) for i, c in enumerate(configs) if c.get("sync_enabled", True) is False
            ]

        set_sync_active(True, service="sharepoint", queue_total=len(active_selected))

        def _worker():
            started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            started_ts = time.time()
            mode_label = "dry-run preview" if dry_run else "sync"
            self._log("sharepoint", f"--- SharePoint {mode_label} started ({trigger}) ---")
            errors: List[str] = []
            new_files: List[str] = []
            planned_files: List[dict] = []
            config_results: List[dict] = []
            total = 0
            processed = 0
            failed = 0
            skipped_cfgs = len(paused_selected)
            skipped_files = 0

            if paused_selected:
                self._log(
                    "sharepoint",
                    f"📋 Active queue: {len(active_selected)} target(s) ({skipped_cfgs} paused excluded up front)",
                )
            elif active_selected:
                self._log("sharepoint", f"📋 Active queue: {len(active_selected)} target(s)")

            try:
                from sp_sync.sharepoint.sync_engine import (
                    _cleanup_playwright,
                    _config_matches_ledger_entry,
                    process_priority_redownloads,
                    start_sync_from_config,
                )

                try:
                    # Phase 0: Give strict top priority to 🔁 Queued for Re-download items BEFORE folder scans!
                    if not cancel_ev.is_set():
                        redl_stats: Dict[str, Any] = {
                            "downloaded": 0,
                            "skipped": 0,
                            "failed": 0,
                            "new_files": [],
                            "planned_files": [],
                            "errors": [],
                            "cancelled": False,
                        }
                        try:
                            only_cfg = active_selected[0][1] if config_index is not None else None
                            redl_count = process_priority_redownloads(
                                fedauth,
                                rtfa,
                                site_url,
                                configs,
                                log_callback=lambda msg: self._log("sharepoint", msg),
                                only_config=only_cfg,
                                cancel_event=cancel_ev,
                                dry_run=dry_run,
                                keep_browser_open=True,
                                stats=redl_stats,
                            )
                            total += int(redl_count or 0)
                            new_files.extend(redl_stats.get("new_files") or [])
                            planned_files.extend(redl_stats.get("planned_files") or [])
                            if redl_stats.get("errors"):
                                errors.extend(redl_stats["errors"][:5])
                        except Exception as redl_exc:
                            errors.append(f"Priority re-download: {redl_exc}")
                            self._log("sharepoint", f"❌ Priority re-download error: {redl_exc}")

                    # If any pending re-downloads remain (e.g. without rel_url), promote their active configs first
                    ordered_active = list(active_selected)
                    try:
                        from sp_sync.db.store import get_store

                        rem_pending = get_store().ledger_pending_redownloads()
                        if rem_pending and len(ordered_active) > 1:
                            prio = []
                            rest = []
                            for pair in ordered_active:
                                if any(_config_matches_ledger_entry(pair[1], pe) for pe in rem_pending):
                                    prio.append(pair)
                                else:
                                    rest.append(pair)
                            ordered_active = prio + rest
                    except Exception:
                        pass

                    for _idx, cfg in ordered_active:
                        if cancel_ev.is_set():
                            break
                        cfg_name = cfg.get("name", "SharePoint")
                        processed += 1
                        set_sync_status_message(f"Syncing {cfg_name}...", service="sharepoint")
                        self._log("sharepoint", f"📂 [{processed}/{len(ordered_active)}] Syncing: {cfg_name}...")
                        cfg_stats: Dict[str, Any] = {
                            "downloaded": 0,
                            "skipped": 0,
                            "failed": 0,
                            "new_files": [],
                            "planned_files": [],
                            "errors": [],
                            "cancelled": False,
                        }
                        try:
                            count = start_sync_from_config(
                                fedauth,
                                rtfa,
                                site_url,
                                cfg,
                                log_callback=lambda msg: self._log("sharepoint", msg),
                                cancel_event=cancel_ev,
                                dry_run=dry_run,
                                keep_browser_open=True,
                                stats=cfg_stats,
                            )
                            downloaded = int(count or 0)
                            total += downloaded
                            skipped_files += int(cfg_stats.get("skipped") or 0)
                            new_files.extend(cfg_stats.get("new_files") or [])
                            planned_files.extend(cfg_stats.get("planned_files") or [])
                            if cfg_stats.get("errors"):
                                failed += 1
                                errs = [f"{cfg_name}: {e}" for e in cfg_stats["errors"][:5]]
                                errors.extend(errs)
                                config_results.append(
                                    {
                                        "name": cfg_name,
                                        "status": "partial" if downloaded > 0 else "failed",
                                        "downloaded_files": downloaded,
                                        "error": "; ".join(cfg_stats["errors"][:2]),
                                    }
                                )
                            else:
                                config_results.append(
                                    {
                                        "name": cfg_name,
                                        "status": "cancelled" if cfg_stats.get("cancelled") else "ok",
                                        "downloaded_files": downloaded,
                                        "error": "",
                                    }
                                )
                        except Exception as cfg_exc:
                            failed += 1
                            msg = f"{cfg_name}: {cfg_exc}"
                            errors.append(msg)
                            config_results.append(
                                {"name": cfg_name, "status": "failed", "downloaded_files": 0, "error": str(cfg_exc)}
                            )
                            self._log("sharepoint", f"❌ Config failed: {msg}")
                finally:
                    _cleanup_playwright()

                if cancel_ev.is_set():
                    result = f"🛑 Stopped by user. Processed {processed} folder(s), downloaded {total} file(s)."
                elif dry_run:
                    result = f"🔍 Dry-run done. {total} file(s) would be downloaded ({skipped_files} skipped)."
                elif errors:
                    result = f"⚠️ Done with errors. Downloaded {total} new file(s). Failed configs: {failed}."
                else:
                    result = f"✅ Done. Downloaded {total} new file(s)."
                self._log("sharepoint", result)
                with self._lock:
                    self.status["sharepoint"]["last_result"] = result
            except Exception as e:
                error_msg = f"❌ Error: {e}"
                self._log("sharepoint", error_msg)
                with self._lock:
                    self.status["sharepoint"]["last_result"] = error_msg
                errors.append(str(e))
                failed = max(1, failed)
            finally:
                finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                elapsed = round(time.time() - started_ts, 3)
                cancelled = cancel_ev.is_set()
                with self._lock:
                    self.status["sharepoint"]["running"] = False
                    self.status["sharepoint"]["stopping"] = False
                    self.status["sharepoint"]["last_run"] = finished_at
                    last_msg = self.status["sharepoint"]["last_result"]
                set_sync_active(False, service="sharepoint")
                status_val = (
                    "cancelled"
                    if cancelled
                    else ("dry_run" if dry_run else ("success" if not errors else ("partial" if processed > failed else "error")))
                )
                report = {
                    "service": "sharepoint",
                    "trigger": trigger,
                    "dry_run": dry_run,
                    "status": status_val,
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "elapsed_sec": elapsed,
                    "downloaded_files": total,
                    "skipped_files": skipped_files,
                    "new_files": new_files[:200],
                    "planned_files": planned_files[:200] if dry_run else [],
                    "configs_total": len(active_selected) + len(paused_selected),
                    "configs_processed": processed,
                    "configs_failed": failed,
                    "configs_skipped": skipped_cfgs,
                    "skipped_reasons": {"disabled": skipped_cfgs} if skipped_cfgs else {},
                    "config_results": config_results,
                    "errors": errors[:20],
                    "message": last_msg,
                }
                self._save_report("sharepoint", report)
                if on_complete:
                    try:
                        on_complete(report)
                    except Exception:
                        pass

        t = threading.Thread(target=_worker, daemon=True, name="SharePointSyncWorker")
        with self._lock:
            self._threads["sharepoint"] = t
        t.start()
        return True, "Sync started in the background...", t

    def start_gdrive(
        self,
        *,
        configs: List[dict],
        trigger: str = "manual",
        dry_run: bool = False,
        config_index: Optional[int] = None,
        on_complete: Optional[Callable[[dict], None]] = None,
    ) -> Tuple[bool, str, Optional[threading.Thread]]:
        """Atomically reserve and start a Google Drive sync background thread."""
        with self._lock:
            if self.status["gdrive"]["running"]:
                return False, "A Google Drive sync is already running.", None
            self.status["gdrive"]["running"] = True
            self.status["gdrive"]["stopping"] = False
            cancel_ev = threading.Event()
            self._cancel_events["gdrive"] = cancel_ev

        if config_index is not None:
            if 0 <= config_index < len(configs):
                active_selected = [(config_index, configs[config_index])]
                paused_selected: List[Tuple[int, dict]] = []
            else:
                with self._lock:
                    self.status["gdrive"]["running"] = False
                return False, "Invalid folder index.", None
        else:
            active_selected = [
                (i, c) for i, c in enumerate(configs) if c.get("sync_enabled", True) is not False
            ]
            paused_selected = [
                (i, c) for i, c in enumerate(configs) if c.get("sync_enabled", True) is False
            ]

        set_sync_active(True, service="gdrive", queue_total=len(active_selected))

        def _worker():
            started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            started_ts = time.time()
            mode_label = "dry-run preview" if dry_run else "sync"
            self._log("gdrive", f"--- Google Drive {mode_label} started ({trigger}) ---")
            errors: List[str] = []
            new_files: List[str] = []
            planned_files: List[dict] = []
            config_results: List[dict] = []
            total = 0
            processed = 0
            failed = 0
            skipped_cfgs = len(paused_selected)
            skipped_files = 0

            if paused_selected:
                self._log(
                    "gdrive",
                    f"📋 Active queue: {len(active_selected)} target(s) ({skipped_cfgs} paused excluded up front)",
                )
            elif active_selected:
                self._log("gdrive", f"📋 Active queue: {len(active_selected)} target(s)")

            try:
                from sp_sync.gdrive import start_gdrive_sync

                for _idx, cfg in active_selected:
                    if cancel_ev.is_set():
                        break
                    cfg_name = cfg.get("name", "Google Drive")
                    processed += 1
                    set_sync_status_message(f"Syncing {cfg_name}...", service="gdrive")
                    self._log("gdrive", f"📂 [{processed}/{len(active_selected)}] Syncing: {cfg_name}...")
                    cfg_stats: Dict[str, Any] = {
                        "downloaded": 0,
                        "skipped": 0,
                        "failed": 0,
                        "new_files": [],
                        "planned_files": [],
                        "errors": [],
                        "cancelled": False,
                    }
                    try:
                        count = start_gdrive_sync(
                            cfg["folder_url"],
                            cfg["local_path"],
                            log_callback=lambda msg: self._log("gdrive", msg),
                            cancel_event=cancel_ev,
                            dry_run=dry_run,
                            stats=cfg_stats,
                        )
                        downloaded = int(count or 0)
                        total += downloaded
                        skipped_files += int(cfg_stats.get("skipped") or 0)
                        new_files.extend(cfg_stats.get("new_files") or [])
                        planned_files.extend(cfg_stats.get("planned_files") or [])
                        if cfg_stats.get("errors"):
                            failed += 1
                            errors.extend(f"{cfg_name}: {e}" for e in cfg_stats["errors"][:5])
                            config_results.append(
                                {
                                    "name": cfg_name,
                                    "status": "partial" if downloaded > 0 else "failed",
                                    "downloaded_files": downloaded,
                                    "error": "; ".join(cfg_stats["errors"][:2]),
                                }
                            )
                        else:
                            config_results.append(
                                {
                                    "name": cfg_name,
                                    "status": "cancelled" if cfg_stats.get("cancelled") else "ok",
                                    "downloaded_files": downloaded,
                                    "error": "",
                                }
                            )
                    except Exception as cfg_exc:
                        failed += 1
                        msg = f"{cfg_name}: {cfg_exc}"
                        errors.append(msg)
                        config_results.append(
                            {"name": cfg_name, "status": "failed", "downloaded_files": 0, "error": str(cfg_exc)}
                        )
                        self._log("gdrive", f"❌ Config failed: {msg}")

                if cancel_ev.is_set():
                    result = f"🛑 Stopped by user. Processed {processed} folder(s), downloaded {total} file(s)."
                elif dry_run:
                    result = f"🔍 Dry-run done. {total} file(s) would be downloaded ({skipped_files} skipped)."
                elif errors:
                    result = f"⚠️ Done with errors. Downloaded {total} new file(s). Failed configs: {failed}."
                else:
                    result = f"✅ Done. Downloaded {total} new file(s)."
                self._log("gdrive", result)
                with self._lock:
                    self.status["gdrive"]["last_result"] = result
            except Exception as e:
                error_msg = f"❌ Error: {e}"
                self._log("gdrive", error_msg)
                with self._lock:
                    self.status["gdrive"]["last_result"] = error_msg
                errors.append(str(e))
                failed = max(1, failed)
            finally:
                finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                elapsed = round(time.time() - started_ts, 3)
                cancelled = cancel_ev.is_set()
                with self._lock:
                    self.status["gdrive"]["running"] = False
                    self.status["gdrive"]["stopping"] = False
                    self.status["gdrive"]["last_run"] = finished_at
                    last_msg = self.status["gdrive"]["last_result"]
                set_sync_active(False, service="gdrive")
                status_val = (
                    "cancelled"
                    if cancelled
                    else ("dry_run" if dry_run else ("success" if not errors else ("partial" if processed > failed else "error")))
                )
                report = {
                    "service": "gdrive",
                    "trigger": trigger,
                    "dry_run": dry_run,
                    "status": status_val,
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "elapsed_sec": elapsed,
                    "downloaded_files": total,
                    "skipped_files": skipped_files,
                    "new_files": new_files[:200],
                    "planned_files": planned_files[:200] if dry_run else [],
                    "configs_total": len(active_selected) + len(paused_selected),
                    "configs_processed": processed,
                    "configs_failed": failed,
                    "configs_skipped": skipped_cfgs,
                    "skipped_reasons": {"disabled": skipped_cfgs} if skipped_cfgs else {},
                    "config_results": config_results,
                    "errors": errors[:20],
                    "message": last_msg,
                }
                self._save_report("gdrive", report)
                if on_complete:
                    try:
                        on_complete(report)
                    except Exception:
                        pass

        t = threading.Thread(target=_worker, daemon=True, name="GDriveSyncWorker")
        with self._lock:
            self._threads["gdrive"] = t
        t.start()
        return True, "Sync started in the background...", t
