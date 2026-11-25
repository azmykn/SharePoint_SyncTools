/**
 * Unified Downloaded Lectures Ledger (/ledger)
 * - Folders ordered in configured sync order (#1, #2, ...)
 * - Collapsible / expandable folder cards with Expand All / Collapse All & persisted state
 * - Live search, status filter, Re-download, Cancel, Delete & Open Folder
 */
(function () {
  const SyncApp = window.SyncApp;
  const esc = SyncApp.escapeHtml;
  const COLLAPSE_KEY = "ledger_collapsed_folders_v1";

  function loadCollapsedSet() {
    try {
      const arr = JSON.parse(localStorage.getItem(COLLAPSE_KEY) || "[]");
      return new Set(Array.isArray(arr) ? arr : []);
    } catch (_) {
      return new Set();
    }
  }

  function saveCollapsedSet(set) {
    try {
      localStorage.setItem(COLLAPSE_KEY, JSON.stringify(Array.from(set)));
    } catch (_) {}
  }

  let collapsedFolders = loadCollapsedSet();

  function applyCollapseStateToDom(forceExpand = false) {
    document.querySelectorAll(".ledger-group-card[data-folder]").forEach((card) => {
      const folder = card.getAttribute("data-folder") || "";
      const isCollapsed = !forceExpand && collapsedFolders.has(folder);
      card.classList.toggle("collapsed", isCollapsed);
      const hdr = card.querySelector("[data-ledger-toggle]");
      if (hdr) hdr.setAttribute("aria-expanded", String(!isCollapsed));
    });
  }

  function toggleCard(card) {
    if (!card) return;
    const folder = card.getAttribute("data-folder") || "";
    const willCollapse = !card.classList.contains("collapsed");
    card.classList.toggle("collapsed", willCollapse);
    const hdr = card.querySelector("[data-ledger-toggle]");
    if (hdr) hdr.setAttribute("aria-expanded", String(!willCollapse));
    if (folder) {
      if (willCollapse) collapsedFolders.add(folder);
      else collapsedFolders.delete(folder);
      saveCollapsedSet(collapsedFolders);
    }
  }

  function setAllCollapsed(collapse) {
    document.querySelectorAll(".ledger-group-card[data-folder]").forEach((card) => {
      const folder = card.getAttribute("data-folder") || "";
      card.classList.toggle("collapsed", collapse);
      const hdr = card.querySelector("[data-ledger-toggle]");
      if (hdr) hdr.setAttribute("aria-expanded", String(!collapse));
      if (folder) {
        if (collapse) collapsedFolders.add(folder);
        else collapsedFolders.delete(folder);
      }
    });
    saveCollapsedSet(collapsedFolders);
  }

  async function loadLedger() {
    const container = document.getElementById("ledger-groups-container");
    if (!container) return;
    const q = (document.getElementById("ledger-search")?.value || "").trim();
    const status = document.getElementById("ledger-status-filter")?.value || "all";
    const isFiltered = Boolean(q || status !== "all");

    const url = `/api/ledger?q=${encodeURIComponent(q)}&status=${encodeURIComponent(status)}`;
    try {
      const { ok, data } = await SyncApp.api(url);
      if (!ok || !data.success) {
        container.innerHTML = `<div class="error-panel">${esc(data.error || "Could not load ledger")}</div>`;
        return;
      }
      const st = data.stats || {};
      const navBadge = document.getElementById("nav-ledger-count");
      if (navBadge && !isFiltered) navBadge.textContent = String(st.total_items || 0);

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
      const redlMissingBtn = document.getElementById("btn-ledger-redl-missing");
      if (redlMissingBtn) {
        redlMissingBtn.style.display = st.missing_count > 0 ? "inline-flex" : "none";
      }

      const groups = data.groups || [];
      if (!groups.length) {
        container.innerHTML = `<div class="empty-state">${esc(SyncApp.t("ledgerEmpty"))}</div>`;
        return;
      }

      container.innerHTML = groups
        .map((g) => {
          const isCollapsed = !isFiltered && collapsedFolders.has(g.folder || "");
          const orderBadge =
            g.sync_order != null
              ? `<span class="badge badge-accent" title="Sync order">#${Number(g.sync_order)}</span>`
              : "";
          const pausedBadge =
            g.sync_enabled === false
              ? `<span class="badge badge-warn">${esc(SyncApp.t("paused"))}</span>`
              : "";
          const pendingBadge = g.pending_count ? `<span class="badge badge-warn">🔁 ${Number(g.pending_count)}</span>` : "";
          const missingBadge = g.missing_count ? `<span class="badge badge-err">✘ ${Number(g.missing_count)}</span>` : "";

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
                      ${it.local_exists ? `<button type="button" class="btn btn-outline btn-small" data-ledger-action="trim" data-key="${esc(it.item_key)}" title="${esc(SyncApp.t("trimSilenceBtn"))}">✂️</button>` : ""}
                      <button type="button" class="btn btn-danger btn-small" data-ledger-action="delete" data-key="${esc(it.item_key)}" title="${esc(SyncApp.t("ledgerBtnDelete"))}">🗑️</button>
                    </div>
                  </td>
                </tr>
              `;
            })
            .join("");

          return `
            <div class="card ledger-group-card ${isCollapsed ? "collapsed" : ""}" data-folder="${esc(g.folder)}">
              <div class="ledger-group-header" data-ledger-toggle role="button" tabindex="0" aria-expanded="${isCollapsed ? "false" : "true"}">
                <div class="ledger-group-title-wrap">
                  <span class="ledger-chevron" aria-hidden="true">▾</span>
                  ${orderBadge}
                  ${pausedBadge}
                  <span class="card-header" style="margin:0;">📚 ${esc(g.subject)}</span>
                  <span class="badge">${(g.items || []).length}</span>
                  <span class="muted" style="font-size:0.82rem;">${Number(g.total_mb || 0)} MB</span>
                  ${pendingBadge}
                  ${missingBadge}
                </div>
                <button type="button" class="btn btn-outline btn-small" data-ledger-open="${esc(g.folder)}">📂 ${esc(SyncApp.t("openLocal"))}</button>
              </div>
              <div class="ledger-group-body">
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
            </div>
          `;
        })
        .join("");
    } catch (_) {
      container.innerHTML = `<div class="error-panel">Connection error while loading ledger.</div>`;
    }
  }

  async function refreshUntrimmedBadge() {
    const btn = document.getElementById("btn-ledger-clean-untrimmed");
    const lbl = document.getElementById("ledger-untrimmed-label");
    if (!btn || !lbl) return;
    try {
      const { ok, data } = await SyncApp.api("/api/ledger/untrimmed");
      if (ok && data.success && data.files > 0) {
        btn.style.display = "inline-flex";
        btn.dataset.files = String(data.files);
        btn.dataset.mb = String(data.size_mb);
        lbl.textContent = `${SyncApp.t("cleanUntrimmed")} (${data.files} • ${data.size_mb} MB)`;
      } else {
        btn.style.display = "none";
      }
    } catch (_) {}
  }

  document.addEventListener("DOMContentLoaded", () => {
    applyCollapseStateToDom(false);
    refreshUntrimmedBadge();

    const debouncedLoad = SyncApp.debounce(loadLedger, 220);
    document.getElementById("ledger-search")?.addEventListener("input", debouncedLoad);
    document.getElementById("ledger-status-filter")?.addEventListener("change", loadLedger);
    document.getElementById("btn-ledger-refresh")?.addEventListener("click", () => {
      loadLedger();
      refreshUntrimmedBadge();
    });
    document.getElementById("btn-ledger-expand-all")?.addEventListener("click", () => setAllCollapsed(false));
    document.getElementById("btn-ledger-collapse-all")?.addEventListener("click", () => setAllCollapsed(true));

    document.getElementById("btn-ledger-redl-missing")?.addEventListener("click", async () => {
      const yes = await SyncApp.confirm("Queue all missing lectures for priority re-download on the next sync?");
      if (!yes) return;
      const { ok, data } = await SyncApp.api("/api/ledger/redownload_missing", { method: "POST" });
      if (ok && data.success) {
        SyncApp.toast(`🔁 Queued ${data.queued} missing lecture(s) for priority re-download`, "success");
        loadLedger();
      } else {
        SyncApp.toast(data.error || "Action failed", "error");
      }
    });

    document.getElementById("btn-ledger-clean-untrimmed")?.addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      const files = btn.dataset.files || "0";
      const mb = btn.dataset.mb || "0";
      const yes = await SyncApp.confirm(
        `Delete ${files} .untrimmed.mp4 backup file(s) (${mb} MB)? Only backups with a valid trimmed .mp4 copy are removed.`
      );
      if (!yes) return;
      const { ok, data } = await SyncApp.api("/api/ledger/cleanup_untrimmed", { method: "POST" });
      if (ok && data.success) {
        SyncApp.toast(`🧹 Removed ${data.removed} backup(s), freed ${data.freed_mb} MB`, "success");
        refreshUntrimmedBadge();
      } else {
        SyncApp.toast(data.error || "Cleanup failed", "error");
      }
    });

    const container = document.getElementById("ledger-groups-container");
    container?.addEventListener("click", async (e) => {
      const openBtn = e.target.closest("[data-ledger-open]");
      if (openBtn) {
        e.stopPropagation();
        SyncApp.openLocalFolder(openBtn.getAttribute("data-ledger-open"));
        return;
      }

      const toggleHdr = e.target.closest("[data-ledger-toggle]");
      if (toggleHdr) {
        toggleCard(toggleHdr.closest(".ledger-group-card"));
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
      } else if (action === "trim") {
        const yes = await SyncApp.confirm("Run leading/trailing silence trimming on this lecture in the background?");
        if (!yes) return;
        const { ok, data } = await SyncApp.api("/api/ledger/trim_silence", {
          method: "POST",
          body: { item_key: key },
        });
        if (ok && data.success) {
          SyncApp.toast(`✂️ Silence trimming started for ${data.file} (check Activity Log)`, "success");
          setTimeout(refreshUntrimmedBadge, 5000);
        } else {
          SyncApp.toast(data.error || "Could not start silence trim", "error");
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

    container?.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        const toggleHdr = e.target.closest("[data-ledger-toggle]");
        if (toggleHdr && !e.target.closest("button")) {
          e.preventDefault();
          toggleCard(toggleHdr.closest(".ledger-group-card"));
        }
      }
    });

    window.addEventListener("app:langchange", () => loadLedger());
    window.addEventListener("app:syncfinished", () => loadLedger());
  });
})();
