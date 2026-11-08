# Module Improvement Backlog (Issue-Ready)

## P0 — Critical

### P0-1: Add sync run summary model
**Goal:** Persist last sync run metrics and failure reasons per service.

**Tasks**
- Add `last_sync_report` storage in SQLite.
- Capture `downloaded`, `skipped`, `failed`, `elapsed_sec`, `errors[]`.
- Expose via `GET /sync/report?service=sharepoint|gdrive`.

**Done when**
- UI can show last report without reading logs.
- Unit tests cover report write/read path.

---

### P0-2: Defensive network wrappers for download calls
**Goal:** Apply consistent timeout/retry policy to download HTTP operations.

**Tasks**
- Add shared HTTP GET wrapper in sync engines.
- Retry only transient statuses (`429`, `500`, `502`, `503`, `504`).
- Keep max retry bounded and observable in logs.

**Done when**
- Transient failures recover automatically in test stubs.

---

## P1 — High

### P1-1: Structured API error codes coverage
**Goal:** Standardize error codes for all write routes.

**Tasks**
- Expand `_json_error` usage to remaining endpoints.
- Document error codes in README.

**Done when**
- Every failure response includes stable `code` for frontend handling.

---

### P1-2: Scheduler observability
**Goal:** Make scheduler decisions visible and auditable.

**Tasks**
- Add scheduler state endpoint fields: `is_due`, `daemon_alive`.
- Add route to force-run scheduler callback manually (admin/testing).

**Done when**
- Tests validate due calculation and daemon lifecycle visibility.

---

### P1-3: SharePoint browse diagnostics enrichment
**Goal:** Improve operator debugging when browse fails.

**Tasks**
- Include normalized request URL/method mode in error payload.
- Keep body snippets capped and safe.

**Done when**
- Support can diagnose 401/403/404 without reproducing locally.

---

## P2 — Medium

### P2-1: UX improvements for long sync runs
- Progress card status states: idle/running/warning/error.
- Add “last updated” timestamp and stale-data hint.

### P2-2: Dry-run mode
- Add dry-run API switch to list what would download without writing files.

### P2-3: Config export/import
- JSON backup & restore for SharePoint/GDrive config rows.

---

## Suggested sprint ordering
- **Sprint 1:** P0-1, P0-2
- **Sprint 2:** P1-1, P1-2
- **Sprint 3:** P1-3, P2-1
- **Sprint 4:** P2-2, P2-3
