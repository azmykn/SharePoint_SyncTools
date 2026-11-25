/**
 * Unified Downloaded Lectures Ledger (/ledger)
 * Enhances server-rendered table with live search, status filter, Re-download, Cancel, Delete & Open Folder.
 */
(function () {
  const SyncApp = window.SyncApp;
  const esc = SyncApp.escapeHtml;

  async function loadLedger() {
    const container = document.getElementById("ledger-groups-container");
    if (!container) return;
    const q = (document.getElementById("ledger-search")?.value || "").trim();
    const status = document.getElementById("ledger-status-filter")?.value || "all";

    const url = `/api/ledger?q=${encodeURIComponent(q)}&status=${encodeURIComponent(status)}`;
    try {
      const { ok, data } = await SyncApp.api(url);
      if (!ok || !data.success) {
        container.innerHTML = `<div class="error-panel">${esc(data.error || "Could not load ledger")}</div>`;
        return;
      }
      const st = data.stats || {};
      const navBadge = document.getElementById("nav-ledger-count");
      if (navBadge && !q && status === "all") navBadge.textContent = String(st.total_items || 0);

      const statsBar = document.getElementById("ledger-stats-bar");
      if (statsBar) {
        statsBar.innerHTML = `
          <span class="badge badge-accent">📚 ${Number(st.subjects || 0)}</span>
          <span class="badge">🎬 ${Number(st.total_items || 0)}</span>
          <span class="badge">💾 ${Number(st.total_mb || 0)} MB</span>
          ${st.missing_count ? `<span class="badge badge-warn">⚠️ ${st.missing_count}</span>` : ""}
          ${st.pending_redownload ? `<span class="badge badge-ok">🔁 ${st.pending_redownload}</span>` : ""}
        `;
      }

      const groups = data.groups || [];
      if (!groups.length) {
        container.innerHTML = `<div class="empty-state">${esc(SyncApp.t("ledgerEmpty"))}</div>`;
        return;
      }

      container.innerHTML = groups
        .map((g) => {
          const rowsHtml = (g.items || [])
            .map((it) => {
              const statusBadge = it.redownload_requested
                ? `<span class="badge badge-warn">🔁 ${esc(SyncApp.t("ledgerStatusPending"))}</span>`
                : it.local_exists
                ? `<span class="badge badge-ok">✔ ${esc(SyncApp.t("ledgerStatusOk"))}</span>`
                : `<span class="badge badge-err">✘ ${esc(SyncApp.t("ledgerStatusMissing"))}</span>`;

              const actionBtn = it.redownload_requested
                ? `<button type="button" class="btn btn-outline btn-small" data-ledger-action="cancel-redl" data-key="${esc(it.item_key)}">${esc(SyncApp.t("ledgerBtnCancelRedl"))}</button>`
                : `<button type="button" class="btn btn-small" data-ledger-action="redl" data-key="${esc(it.item_key)}">${esc(SyncApp.t("ledgerBtnRedl"))}</button>`;

              return `
                <tr>
                  <td>${it.seq ? String(it.seq).padStart(2, "0") : "—"}</td>
                  <td>
                    <div style="font-weight:600;">${esc(it.name)}</div>
                    ${it.original_name && it.original_name !== it.name ? `<div class="muted" style="font-size:0.74rem;">${esc(it.original_name)}</div>` : ""}
                  </td>
                  <td>${esc(it.date || "—")}</td>
                  <td>${Number(it.size_mb || 0)} MB</td>
                  <td>${statusBadge}</td>
                  <td>
                    <div class="toolbar-row">
                      ${actionBtn}
                      <button type="button" class="btn btn-danger btn-small" data-ledger-action="delete" data-key="${esc(it.item_key)}" title="${esc(SyncApp.t("ledgerBtnDelete"))}">🗑️</button>
                    </div>
                  </td>
                </tr>
              `;
            })
            .join("");

          return `
            <div class="card">
              <div class="card-header-row">
                <div class="card-header">
                  📚 ${esc(g.subject)}
                  <span class="badge">${(g.items || []).length}</span>
                  <span class="muted" style="font-weight:400;font-size:0.82rem;">${Number(g.total_mb || 0)} MB</span>
                </div>
                <button type="button" class="btn btn-outline btn-small" data-ledger-open="${esc(g.folder)}">📂 ${esc(SyncApp.t("openLocal"))}</button>
              </div>
              <div class="table-wrap">
                <table class="data-table">
                  <thead>
                    <tr>
                      <th>${esc(SyncApp.t("ledgerColSeq"))}</th>
                      <th>${esc(SyncApp.t("ledgerColName"))}</th>
                      <th>${esc(SyncApp.t("ledgerColDate"))}</th>
                      <th>${esc(SyncApp.t("ledgerColSize"))}</th>
                      <th>${esc(SyncApp.t("ledgerColStatus"))}</th>
                      <th>${esc(SyncApp.t("ledgerColActions"))}</th>
                    </tr>
                  </thead>
                  <tbody>${rowsHtml}</tbody>
                </table>
              </div>
            </div>
          `;
        })
        .join("");
    } catch (_) {
      container.innerHTML = `<div class="error-panel">Connection error while loading ledger.</div>`;
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    const debouncedLoad = SyncApp.debounce(loadLedger, 220);
    document.getElementById("ledger-search")?.addEventListener("input", debouncedLoad);
    document.getElementById("ledger-status-filter")?.addEventListener("change", loadLedger);
    document.getElementById("btn-ledger-refresh")?.addEventListener("click", loadLedger);

    document.getElementById("ledger-groups-container")?.addEventListener("click", async (e) => {
      const openBtn = e.target.closest("[data-ledger-open]");
      if (openBtn) {
        SyncApp.openLocalFolder(openBtn.getAttribute("data-ledger-open"));
        return;
      }
      const btn = e.target.closest("[data-ledger-action]");
      if (!btn) return;
      const action = btn.getAttribute("data-ledger-action");
      const key = btn.getAttribute("data-key");
      if (!key) return;

      if (action === "redl" || action === "cancel-redl") {
        const cancel = action === "cancel-redl";
        if (!cancel) {
          const yes = await SyncApp.confirm("Queue this lecture for re-download on the next sync?");
          if (!yes) return;
        }
        const { ok, data } = await SyncApp.api("/api/ledger/redownload", {
          method: "POST",
          body: { item_key: key, cancel },
        });
        if (ok && data.success) {
          SyncApp.toast(cancel ? "Re-download cancelled" : "Queued for next sync", "success");
          loadLedger();
        } else {
          SyncApp.toast(data.error || "Action failed", "error");
        }
      } else if (action === "delete") {
        const yes = await SyncApp.confirm("Remove this item from the download ledger? (Does not delete the local file.)");
        if (!yes) return;
        const { ok, data } = await SyncApp.api("/api/ledger/delete", {
          method: "POST",
          body: { item_key: key },
        });
        if (ok && data.success) {
          SyncApp.toast("Ledger entry removed", "success");
          loadLedger();
        } else {
          SyncApp.toast(data.error || "Delete failed", "error");
        }
      }
    });

    window.addEventListener("app:langchange", () => loadLedger());
    window.addEventListener("app:syncfinished", () => loadLedger());
  });
})();
