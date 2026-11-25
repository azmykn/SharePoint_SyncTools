/**
 * Overview Dashboard (/) — 4 Clickable KPIs, Unified Sync Summary & History, and Live Activity Peek
 */
(function () {
  const SyncApp = window.SyncApp;
  const esc = SyncApp.escapeHtml;
  const FILTERS_KEY = "syncReportFilters.v2";

  function getReportFilters() {
    const service = document.getElementById("sync-report-service-filter")?.value || "all";
    const limit = parseInt(document.getElementById("sync-report-limit-filter")?.value || "6", 10) || 6;
    const q = (document.getElementById("sync-report-search")?.value || "").trim();
    return { service, limit, q };
  }

  function saveReportFilters() {
    try {
      const f = getReportFilters();
      localStorage.setItem(FILTERS_KEY, JSON.stringify(f));
    } catch (_) {}
  }

  function restoreReportFilters() {
    const svcEl = document.getElementById("sync-report-service-filter");
    const limEl = document.getElementById("sync-report-limit-filter");
    const qEl = document.getElementById("sync-report-search");
    try {
      const saved = JSON.parse(localStorage.getItem(FILTERS_KEY) || "null");
      if (saved) {
        if (svcEl && ["all", "sharepoint", "gdrive"].includes(saved.service)) svcEl.value = saved.service;
        if (limEl && ["6", "10", "20", "50"].includes(String(saved.limit))) limEl.value = String(saved.limit);
        if (qEl && saved.q) qEl.value = saved.q;
      }
    } catch (_) {}
  }

  function statusBadgeClass(st) {
    const s = String(st || "").toLowerCase();
    if (s === "success" || s === "dry_run") return "badge-ok";
    if (s === "partial" || s === "cancelled") return "badge-warn";
    return "badge-err";
  }

  function renderReportCard(label, report) {
    const statusRaw = String(report.status || "unknown").toLowerCase();
    const errs = Array.isArray(report.errors) ? report.errors : [];
    const planned = Array.isArray(report.planned_files) ? report.planned_files : [];
    const errHtml = errs.length
      ? `<div style="margin-top:6px;color:var(--danger);font-size:0.82rem;">Errors: ${esc(errs.slice(0, 3).join(" | "))}</div>`
      : "";
    const plannedHtml =
      report.dry_run && planned.length
        ? `<details style="margin-top:8px;"><summary style="cursor:pointer;color:var(--accent);font-weight:600;">🔍 Dry-run files (${planned.length})</summary><ul style="margin:6px 0 0 18px;font-size:0.8rem;">${planned
            .slice(0, 30)
            .map((p) => `<li>${esc(p.name)} (${esc(SyncApp.formatBytes(p.size))})</li>`)
            .join("")}</ul></details>`
        : "";

    return `
      <div class="report-box">
        <div class="card-header-row" style="margin-bottom:6px;">
          <strong>${esc(label)}</strong>
          <span class="badge ${statusBadgeClass(statusRaw)}">${esc(statusRaw.toUpperCase())}</span>
        </div>
        <div>
          Downloaded: <strong>${Number(report.downloaded_files || 0)}</strong>
          ${report.skipped_files != null ? `• Skipped: <strong>${Number(report.skipped_files)}</strong>` : ""}
          • Targets: <strong>${Number(report.configs_processed || 0)}/${Number(report.configs_total || 0)}</strong>
          • Failed: <strong>${Number(report.configs_failed || 0)}</strong>
        </div>
        <div class="muted" style="margin-top:4px;">
          Trigger: ${esc(report.trigger || "-")} • Duration: ${esc(report.elapsed_sec ?? "-")}s • Finished: ${esc(report.finished_at || "-")}
        </div>
        ${errHtml}
        ${plannedHtml}
      </div>
    `;
  }

  function renderHistoryList(items) {
    if (!Array.isArray(items) || !items.length) {
      return `<div class="muted">${esc(SyncApp.t("noHistoryYet"))}</div>`;
    }
    return items
      .map((it) => {
        const status = String(it.status || "unknown").toLowerCase();
        return `
          <div class="history-row">
            <span class="muted">${esc(it.finished_at || it.started_at || "-")}</span>
            <strong>${esc(it.service || "unknown")}</strong>
            <span class="badge ${statusBadgeClass(status)}">${esc(status)}</span>
            <span>↓ ${Number(it.downloaded_files || 0)}</span>
            <span class="muted">(${esc(it.trigger || "-")})</span>
          </div>
        `;
      })
      .join("");
  }

  async function refreshSyncReport() {
    if (document.hidden) return;
    try {
      const { service, limit, q } = getReportFilters();
      const repUrl = service === "all" ? "/sync/report" : `/sync/report?service=${encodeURIComponent(service)}`;
      const histUrl = `/sync/report/history?service=${encodeURIComponent(service)}&limit=${encodeURIComponent(String(limit))}&q=${encodeURIComponent(q)}`;
      const [repRes, histRes] = await Promise.all([SyncApp.api(repUrl), SyncApp.api(histUrl)]);

      const contentEl = document.getElementById("sync-report-content");
      const histEl = document.getElementById("sync-report-history");
      if (repRes.ok && repRes.data.success && contentEl) {
        let html = "";
        if (service === "all") {
          const sp = (repRes.data.reports && repRes.data.reports.sharepoint) || {};
          const gd = (repRes.data.reports && repRes.data.reports.gdrive) || {};
          if (Object.keys(sp).length) html += renderReportCard("SharePoint", sp);
          if (Object.keys(gd).length) html += renderReportCard("Google Drive", gd);
        } else {
          const rep = repRes.data.report || {};
          if (Object.keys(rep).length) {
            html += renderReportCard(service === "sharepoint" ? "SharePoint" : "Google Drive", rep);
          }
        }
        contentEl.innerHTML = html || `<div class="muted">${esc(SyncApp.t("noReportYet"))}</div>`;
      }
      if (histRes.ok && histRes.data.success && histEl) {
        histEl.innerHTML = renderHistoryList(histRes.data.items || []);
      }
    } catch (_) {}
  }

  async function refreshOverviewKpis() {
    try {
      const [healthRes, ledgerRes, schedRes] = await Promise.all([
        SyncApp.api("/api/system/health"),
        SyncApp.api("/api/ledger"),
        SyncApp.api("/scheduler/summary"),
      ]);

      if (healthRes.ok && healthRes.data.health) {
        const h = healthRes.data.health;
        const diskEl = document.getElementById("kpi-disk-val");
        const diskSub = document.getElementById("kpi-disk-sub");
        if (diskEl) diskEl.textContent = `${h.disk.free_gb} GB`;
        if (diskSub) {
          diskSub.textContent = h.disk.low_space
            ? SyncApp.t("diskLowSpace", { tmp: h.tmp_segments.size_mb })
            : SyncApp.t("diskTotalTmp", { total: h.disk.total_gb, tmp: h.tmp_segments.size_mb });
        }
      }

      if (ledgerRes.ok && ledgerRes.data.stats) {
        const st = ledgerRes.data.stats;
        const lecVal = document.getElementById("kpi-lectures-val");
        const lecSub = document.getElementById("kpi-lectures-sub");
        const navBadge = document.getElementById("nav-ledger-count");
        if (lecVal) lecVal.textContent = String(st.total_items);
        if (lecSub) lecSub.textContent = SyncApp.t("subjectsSummary", { subjects: st.subjects, mb: st.total_mb });
        if (navBadge) navBadge.textContent = String(st.total_items);
      }

      if (schedRes.ok && schedRes.data.success) {
        const cfg = schedRes.data.settings || {};
        const sum = schedRes.data.summary || {};
        const scVal = document.getElementById("kpi-sched-val");
        const scSub = document.getElementById("kpi-sched-sub");
        if (scVal) {
          scVal.innerHTML = cfg.enabled
            ? `<span style="color:var(--success);">${esc(SyncApp.t("enabled"))}</span>`
            : `<span style="color:var(--text-secondary);">${esc(SyncApp.t("disabled"))}</span>`;
        }
        if (scSub) {
          scSub.textContent = cfg.enabled
            ? (cfg.schedule_mode === "daily" ? `${SyncApp.t("setSchedModeDaily")} (${cfg.daily_time})` : `${cfg.interval_hours}h`) +
              (cfg.next_run ? ` • ${cfg.next_run}` : "")
            : SyncApp.t("manualOnly");
        }
        const sumInline = document.getElementById("scheduler-last-summary-inline");
        if (sumInline) {
          if (sum && sum.finished_at) {
            sumInline.innerHTML = `⏰ <strong>${esc(sum.finished_at)}</strong> • ${esc(sum.target || "all")} • ↓ <strong>${Number(sum.downloaded_files || 0)}</strong>`;
          } else {
            sumInline.textContent = "";
          }
        }
      }
    } catch (_) {}
  }

  async function refreshMiniLogs() {
    if (document.hidden) return;
    const box = document.getElementById("overview-mini-logs");
    if (!box) return;
    try {
      const { ok, data } = await SyncApp.api("/api/logs?limit=6");
      if (!ok || !Array.isArray(data)) return;
      if (!data.length) {
        box.innerHTML = `<div class="muted">${esc(SyncApp.t("logEmpty"))}</div>`;
        return;
      }
      // Show newest on top!
      const newest = data.slice(-6).reverse();
      box.innerHTML = newest
        .map((e) => {
          const t = String(e.time || "").split(" ")[1] || String(e.time || "");
          const src = e.source === "sharepoint" ? "[SP]" : e.source === "gdrive" ? "[GD]" : "[SYS]";
          const cls = e.source === "sharepoint" ? "log-source-sp" : e.source === "gdrive" ? "log-source-gd" : "log-source-sys";
          return `<div class="log-line"><span class="log-time">${esc(t)}</span><span class="${cls}">${src}</span><span class="log-msg">${esc(e.msg || "")}</span></div>`;
        })
        .join("");
    } catch (_) {}
  }

  document.addEventListener("DOMContentLoaded", () => {
    restoreReportFilters();

    const onFilterChange = SyncApp.debounce(() => {
      saveReportFilters();
      refreshSyncReport();
    }, 220);

    document.getElementById("sync-report-service-filter")?.addEventListener("change", () => {
      saveReportFilters();
      refreshSyncReport();
    });
    document.getElementById("sync-report-limit-filter")?.addEventListener("change", () => {
      saveReportFilters();
      refreshSyncReport();
    });
    document.getElementById("sync-report-search")?.addEventListener("input", onFilterChange);
    document.getElementById("btn-report-refresh")?.addEventListener("click", refreshSyncReport);
    document.getElementById("btn-report-export")?.addEventListener("click", () => {
      const { service, limit, q } = getReportFilters();
      window.open(
        `/sync/report/history/export?service=${encodeURIComponent(service)}&limit=${encodeURIComponent(String(limit))}&q=${encodeURIComponent(q)}`,
        "_blank"
      );
    });
    document.getElementById("btn-report-clear")?.addEventListener("click", async () => {
      const { service } = getReportFilters();
      const ok = await SyncApp.confirm(`Clear sync report & history (${service})?`);
      if (!ok) return;
      const res = await SyncApp.api("/sync/report/clear", { method: "POST", body: { service } });
      if (res.ok && res.data.success) {
        SyncApp.toast("Sync report cleared", "success");
        refreshSyncReport();
      } else {
        SyncApp.toast(res.data.error || "Could not clear report", "error");
      }
    });

    window.addEventListener("app:syncfinished", () => {
      refreshSyncReport();
      refreshOverviewKpis();
      refreshMiniLogs();
    });

    window.addEventListener("app:langchange", () => {
      refreshOverviewKpis();
      refreshSyncReport();
    });

    refreshSyncReport();
    refreshOverviewKpis();
    refreshMiniLogs();
    setInterval(refreshMiniLogs, 3500);
    setInterval(refreshSyncReport, 12000);
  });
})();
