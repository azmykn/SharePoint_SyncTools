# Module Improvement Execution Plan

This document translates the requested "develop + fix + enhance" work into executable phases with acceptance criteria.

## Scope
- SharePoint sync (`sp_sync/sharepoint/*`)
- Google Drive sync (`sp_sync/gdrive/*`)
- Web API/UI orchestration (`sp_sync/web/app.py`, templates)
- Scheduler and live metrics (`sp_sync/scheduler/*`, `sp_sync/sync_metrics.py`)
- Automated tests under `tests/`

## Phase A — Stabilization (P0/P1)

### A1. API input safety and error consistency
- Add safe JSON parser helper for all POST routes.
- Add unified JSON error payload helper (`success=false`, `error`, optional `code`).
- Ensure invalid inputs return explicit HTTP 400/401/404 where appropriate.

**Acceptance criteria**
- No route crashes on missing or malformed JSON payload.
- Error payloads are consistent across updated routes.

### A2. Scheduler settings validation hardening
- Validate `enabled`, `interval_hours`, and `target` before persistence.
- Reject unsupported intervals and invalid targets with descriptive errors.

**Acceptance criteria**
- Invalid scheduler inputs return deterministic validation messages.
- Valid values persist and compute `next_run` correctly.

### A3. SharePoint listing transient retry
- Add bounded retry with backoff+jitter for transient HTTP failures (`429`, `5xx`) and short network errors.
- Apply retries in OData pagination and v2 shares pagination list calls.

**Acceptance criteria**
- Listing path retries transient failures instead of immediate hard-fail.
- Existing pagination logic remains intact.

## Phase B — Correctness & Data Integrity

### B1. Download integrity policies
- Keep atomic writes for all download modes.
- Enforce content integrity checks before finalize rename.

### B2. Skip/re-download correctness
- Keep size and media validation logic strict for corrupted cases.
- Avoid redundant re-downloads for healthy existing files.

## Phase C — Performance

### C1. Tunable concurrency controls
- Introduce configurable parallel workers for non-dependent file downloads.

### C2. Metrics quality
- Include per-job summary (downloaded/skipped/failed, elapsed, average speed).

## Phase D — UX and Operability

### D1. Sync report endpoint
- Add API endpoint exposing last sync run summary for UI rendering.

### D2. Troubleshooting guide
- Add known-failure matrix and actionable remediation steps.

## Validation Gate (all phases)
1. `./.venv/bin/python -m unittest discover -s tests -v`
2. Manual sanity checks for:
   - `/sp/browse`
   - `/sp/resolve_url`
   - `/scheduler/settings`
   - `/sync/live_metrics`

## Current implementation status
- ✅ Phase A1 implemented
- ✅ Phase A2 implemented
- ✅ Phase A3 implemented
- ✅ Tests added/updated for A1/A2
- 🔄 Remaining phases are planned in backlog (`docs/module_improvement_backlog.md`)
