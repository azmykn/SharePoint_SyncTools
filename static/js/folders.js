/**
 * SharePoint & Google Drive Folder Management
 * - Targets list FIRST with All / Active / Paused filter pills & drag-to-reorder
 * - Add / Edit inside a sleek modal so forms never push folder lists off-screen
 * - Integrated SharePoint sub-tabs (Sync Targets | Explorer & Add from Link)
 */
(function () {
  const SyncApp = window.SyncApp;
  const esc = SyncApp.escapeHtml;

  let spFolders = Array.isArray(window.__INITIAL_SP_FOLDERS__) ? window.__INITIAL_SP_FOLDERS__ : [];
  let gdFolders = Array.isArray(window.__INITIAL_GD_FOLDERS__) ? window.__INITIAL_GD_FOLDERS__ : [];
  let spStateFilter = "all";
  let gdStateFilter = "all";
  let spSelected = new Set();
  let spDragFrom = null;
  let gdDragFrom = null;

  function spEnabled(f) { return f.sync_enabled !== false; }
  function gdEnabled(f) { return f.sync_enabled !== false; }

  function updateSidebarBadges() {
    if (document.getElementById("sp-list")) {
      const spActive = spFolders.filter(spEnabled).length;
      const spBadge = document.getElementById("nav-sp-count");
      if (spBadge) spBadge.textContent = `${spActive}/${spFolders.length}`;
      const numEl = document.getElementById("sp-sync-order-num");
      if (numEl) numEl.textContent = `${spActive}/${spFolders.length}`;
    }
    if (document.getElementById("gd-list")) {
      const gdActive = gdFolders.filter(gdEnabled).length;
      const gdBadge = document.getElementById("nav-gd-count");
      if (gdBadge) gdBadge.textContent = String(gdFolders.length);
      const gdSum = document.getElementById("gd-count-summary");
      if (gdSum) gdSum.textContent = gdFolders.length ? `(${gdActive}/${gdFolders.length})` : "";
    }
  }

  // ==================== SharePoint Sub-View Switcher ====================
  function switchSpView(view) {
    const v = view === "explorer" ? "explorer" : "targets";
    document.querySelectorAll("[data-sp-view]").forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-sp-view") === v);
    });
    document.getElementById("sp-view-targets")?.classList.toggle("active", v === "targets");
    document.getElementById("sp-view-explorer")?.classList.toggle("active", v === "explorer");
    try {
      const targetUrl = v === "explorer" ? "/sharepoint?view=explorer" : "/sharepoint";
      window.history.replaceState(null, "", targetUrl);
    } catch (_) {}
  }

  // ==================== SharePoint Modal & List ====================
  function openSPModal() {
    document.getElementById("sp-form-modal")?.classList.add("open");
    setTimeout(() => document.getElementById("sp-name")?.focus(), 40);
  }

  function closeSPModal() {
    document.getElementById("sp-form-modal")?.classList.remove("open");
    resetSPForm();
  }

  function resetSPForm() {
    document.getElementById("sp-form")?.reset();
    const idxEl = document.getElementById("sp-edit-index");
    if (idxEl) idxEl.value = "-1";
    const btn = document.getElementById("sp-submit-btn");
    if (btn) btn.textContent = SyncApp.t("saveFolder");
    const flatWrap = document.getElementById("sp-local-flat-wrap");
    if (flatWrap) flatWrap.style.display = "";
  }

  function editSP(idx) {
    const f = spFolders[idx];
    if (!f) return;
    document.getElementById("sp-name").value = f.name || "";
    document.getElementById("sp-url").value = f.rel_url || "";
    document.getElementById("sp-path").value = f.local_path || "";
    const etEl = document.getElementById("sp-entry-type");
    if (etEl) etEl.value = f.entry_type === "file" ? "file" : "folder";
    const shareEl = document.getElementById("sp-sharing-link");
    if (shareEl) shareEl.value = f.sharing_link || "";
    document.getElementById("sp-local-flat").checked = !!f.local_flat;
    const voEl = document.getElementById("sp-video-only");
    if (voEl) voEl.checked = !!f.video_only;
    document.getElementById("sp-edit-index").value = String(idx);
    document.getElementById("sp-submit-btn").textContent = SyncApp.t("saveChanges");
    const flatWrap = document.getElementById("sp-local-flat-wrap");
    if (flatWrap) flatWrap.style.display = f.entry_type === "file" ? "none" : "";
    openSPModal();
  }

  function updateSpBulkBar() {
    const bar = document.getElementById("sp-bulk-bar");
    const cnt = document.getElementById("sp-bulk-count");
    if (!bar || !cnt) return;
    // Prune stale indices if list shrunk
    spSelected.forEach((i) => { if (i < 0 || i >= spFolders.length) spSelected.delete(i); });
    const n = spSelected.size;
    bar.style.display = n > 0 ? "flex" : "none";
    cnt.textContent = SyncApp.t("bulkSelectedCount", { n });
  }

  async function runSpBulkAction(action) {
    const indices = Array.from(spSelected).sort((a, b) => a - b);
    if (!indices.length) return;
    const { ok, data } = await SyncApp.api("/sp/bulk_action", {
      method: "POST",
      body: { action, indices },
    });
    if (!ok || !data.success) {
      SyncApp.toast(data.error || "Bulk action failed", "error");
      return;
    }
    if (action === "sync") {
      SyncApp.toast(`🔄 Sync started for ${data.count} selected target(s)`, "success");
      SyncApp.pollLiveMetrics();
    } else {
      if (Array.isArray(data.configs)) spFolders = data.configs;
      spSelected.clear();
      const selAll = document.getElementById("sp-select-all-visible");
      if (selAll) selAll.checked = false;
      renderSPList();
      SyncApp.toast(
        action === "enable"
          ? `▶ Enabled ${data.updated} target(s)`
          : `⏸ Paused ${data.updated} target(s)`,
        "success"
      );
    }
  }

  function renderSPList() {
    const list = document.getElementById("sp-list");
    if (!list) return;
    const filterQ = (document.getElementById("sp-filter-input")?.value || "").trim().toLowerCase();
    const canDrag = !filterQ && spStateFilter === "all";
    updateSidebarBadges();
    updateSpBulkBar();
    list.innerHTML = "";

    const rows = spFolders
      .map((f, idx) => ({ f, idx }))
      .filter(({ f }) => {
        const en = spEnabled(f);
        if (spStateFilter === "active" && !en) return false;
        if (spStateFilter === "paused" && en) return false;
        if (!filterQ) return true;
        return `${f.name || ""} ${f.rel_url || ""} ${f.local_path || ""}`.toLowerCase().includes(filterQ);
      });

    if (!rows.length) {
      list.innerHTML = `<div class="empty-state">${esc(SyncApp.t("noFoldersYet"))}</div>`;
      return;
    }

    rows.forEach(({ f, idx }) => {
      const enabled = spEnabled(f);
      const checked = spSelected.has(idx);
      const card = document.createElement("div");
      card.className = "item-card" + (enabled ? "" : " item-sync-off");
      card.draggable = canDrag;
      card.dataset.index = String(idx);

      const typeBadge =
        f.entry_type === "file"
          ? `<span class="badge badge-accent">${esc(SyncApp.t("expTypeFile"))}</span>`
          : f.local_flat
          ? `<span class="badge">Flat</span>`
          : "";
      const videoBadge = f.video_only ? `<span class="badge">🎬 Video</span>` : "";
      const shareBadge = f.sharing_link ? `<span class="badge badge-ok" title="Has per-folder sharing link">🔗</span>` : "";
      const stateBadge = enabled ? "" : `<span class="badge badge-warn">${esc(SyncApp.t("paused"))}</span>`;

      card.innerHTML = `
        <input type="checkbox" class="sp-select-cb" data-idx="${idx}" ${checked ? "checked" : ""} aria-label="Select ${esc(f.name)}" style="width:16px;height:16px;accent-color:var(--accent);cursor:pointer;flex-shrink:0;">
        <span class="drag-handle" title="Drag to reorder" aria-hidden="true" style="${canDrag ? "" : "opacity:0.25;cursor:default;"}">⋮⋮</span>
        <div class="item-info">
          <div class="item-title">
            <span class="badge">#${idx + 1}</span>
            ${stateBadge}${typeBadge}${videoBadge}${shareBadge}
            <span>${esc(f.name)}</span>
          </div>
          <div class="item-subtitle" dir="ltr">${esc(f.rel_url)} ➔ ${esc(f.local_path)}</div>
        </div>
        <div class="item-actions">
          <button type="button" class="btn btn-success btn-small" data-sp-action="sync" data-idx="${idx}" title="${esc(SyncApp.t("syncSingle"))}">🔄</button>
          <button type="button" class="btn btn-outline btn-small" data-sp-action="open" data-idx="${idx}" title="${esc(SyncApp.t("openLocal"))}">📂</button>
          <button type="button" class="btn ${enabled ? "btn-outline" : "btn-warning"} btn-small" data-sp-action="toggle" data-idx="${idx}" title="${esc(enabled ? SyncApp.t("pauseSync") : SyncApp.t("resumeSync"))}">${enabled ? "⏸" : "▶"}</button>
          <button type="button" class="btn btn-outline btn-small" data-sp-action="edit" data-idx="${idx}" title="${esc(SyncApp.t("edit"))}">✏️</button>
          <button type="button" class="btn btn-danger btn-small" data-sp-action="delete" data-idx="${idx}" title="${esc(SyncApp.t("delete"))}">🗑️</button>
        </div>
      `;

      if (canDrag) {
        card.addEventListener("dragstart", (e) => {
          spDragFrom = idx;
          card.classList.add("dragging");
          e.dataTransfer.effectAllowed = "move";
          try { e.dataTransfer.setData("text/plain", String(idx)); } catch (_) {}
        });
        card.addEventListener("dragend", () => {
          card.classList.remove("dragging");
          spDragFrom = null;
          list.querySelectorAll(".drag-over").forEach((n) => n.classList.remove("drag-over"));
        });
        card.addEventListener("dragover", (e) => { e.preventDefault(); card.classList.add("drag-over"); });
        card.addEventListener("dragleave", () => card.classList.remove("drag-over"));
        card.addEventListener("drop", async (e) => {
          e.preventDefault();
          card.classList.remove("drag-over");
          if (spDragFrom === null || spDragFrom === idx) return;
          const moved = spFolders.splice(spDragFrom, 1)[0];
          spFolders.splice(idx, 0, moved);
          spDragFrom = null;
          renderSPList();
          const res = await SyncApp.api("/sp/reorder_folders", {
            method: "POST",
            body: { configs: spFolders },
          });
          if (res.ok && res.data.success) SyncApp.toast(SyncApp.t("spOrderSaved"), "success");
          else SyncApp.toast(res.data.error || "Could not save order", "error");
        });
      }
      list.appendChild(card);
    });
  }

  async function reloadSPConfigs() {
    const res = await SyncApp.api("/sp/configs");
    if (res.ok && res.data.success && Array.isArray(res.data.configs)) {
      spFolders = res.data.configs;
      renderSPList();
    }
  }

  async function saveSPForm(e) {
    e.preventDefault();
    const idx = parseInt(document.getElementById("sp-edit-index").value, 10);
    const entry_type = document.getElementById("sp-entry-type")?.value === "file" ? "file" : "folder";
    const payload = {
      name: document.getElementById("sp-name").value.trim(),
      rel_url: document.getElementById("sp-url").value.trim(),
      local_path: document.getElementById("sp-path").value.trim(),
      entry_type,
      sharing_link: (document.getElementById("sp-sharing-link")?.value || "").trim(),
      video_only: !!document.getElementById("sp-video-only")?.checked,
    };
    if (entry_type === "folder") {
      payload.local_flat = !!document.getElementById("sp-local-flat").checked;
    }

    const url = idx >= 0 ? "/sp/update_folder" : "/sp/add_folder";
    if (idx >= 0) payload.index = idx;

    const { ok, data } = await SyncApp.api(url, { method: "POST", body: payload });
    if (ok && data.success) {
      if (idx >= 0 && data.config) spFolders[idx] = data.config;
      else if (data.config) spFolders.push(data.config);
      else await reloadSPConfigs();
      renderSPList();
      closeSPModal();
      SyncApp.toast("SharePoint target saved", "success");
    } else {
      SyncApp.toast(data.error || "Failed to save SharePoint target", "error");
    }
  }

  async function triggerSpSync(opts = {}) {
    const { ok, data } = await SyncApp.api("/sp/sync", { method: "POST", body: opts });
    if (ok && data.success) {
      SyncApp.toast(opts.dry_run ? "🔍 SharePoint dry-run started..." : "🔄 SharePoint sync started", "success");
      SyncApp.pollLiveMetrics();
    } else {
      SyncApp.toast(data.error || "Could not start SharePoint sync", "error");
    }
  }

  // ==================== Google Drive Modal & List ====================
  function openGDModal() {
    document.getElementById("gd-form-modal")?.classList.add("open");
    setTimeout(() => document.getElementById("gd-url")?.focus(), 40);
  }

  function closeGDModal() {
    document.getElementById("gd-form-modal")?.classList.remove("open");
    resetGDForm();
  }

  function resetGDForm() {
    document.getElementById("gd-form")?.reset();
    const idxEl = document.getElementById("gd-edit-index");
    if (idxEl) idxEl.value = "-1";
    const btn = document.getElementById("gd-submit-btn");
    if (btn) btn.textContent = SyncApp.t("saveFolder");
  }

  function editGD(idx) {
    const f = gdFolders[idx];
    if (!f) return;
    document.getElementById("gd-name").value = f.name || "";
    document.getElementById("gd-url").value = f.folder_url || "";
    document.getElementById("gd-path").value = f.local_path || "";
    document.getElementById("gd-edit-index").value = String(idx);
    document.getElementById("gd-submit-btn").textContent = SyncApp.t("saveChanges");
    openGDModal();
  }

  function renderGDList() {
    const list = document.getElementById("gd-list");
    if (!list) return;
    const filterQ = (document.getElementById("gd-filter-input")?.value || "").trim().toLowerCase();
    const canDrag = !filterQ && gdStateFilter === "all";
    updateSidebarBadges();
    list.innerHTML = "";

    const rows = gdFolders
      .map((f, idx) => ({ f, idx }))
      .filter(({ f }) => {
        const en = gdEnabled(f);
        if (gdStateFilter === "active" && !en) return false;
        if (gdStateFilter === "paused" && en) return false;
        if (!filterQ) return true;
        return `${f.name || ""} ${f.folder_url || ""} ${f.local_path || ""}`.toLowerCase().includes(filterQ);
      });

    if (!rows.length) {
      list.innerHTML = `<div class="empty-state">${esc(SyncApp.t("noFoldersYet"))}</div>`;
      return;
    }

    rows.forEach(({ f, idx }) => {
      const enabled = gdEnabled(f);
      const card = document.createElement("div");
      card.className = "item-card" + (enabled ? "" : " item-sync-off");
      card.draggable = canDrag;
      card.dataset.index = String(idx);
      const stateBadge = enabled ? "" : `<span class="badge badge-warn">${esc(SyncApp.t("paused"))}</span>`;

      card.innerHTML = `
        <span class="drag-handle" title="Drag to reorder" aria-hidden="true" style="${canDrag ? "" : "opacity:0.25;cursor:default;"}">⋮⋮</span>
        <div class="item-info">
          <div class="item-title">
            <span class="badge">#${idx + 1}</span>
            ${stateBadge}
            <span>${esc(f.name)}</span>
          </div>
          <div class="item-subtitle" dir="ltr">${esc(f.folder_url)} ➔ ${esc(f.local_path)}</div>
        </div>
        <div class="item-actions">
          <button type="button" class="btn btn-success btn-small" data-gd-action="sync" data-idx="${idx}" title="${esc(SyncApp.t("syncSingle"))}">🔄</button>
          <button type="button" class="btn btn-outline btn-small" data-gd-action="open" data-idx="${idx}" title="${esc(SyncApp.t("openLocal"))}">📂</button>
          <button type="button" class="btn ${enabled ? "btn-outline" : "btn-warning"} btn-small" data-gd-action="toggle" data-idx="${idx}" title="${esc(enabled ? SyncApp.t("pauseSync") : SyncApp.t("resumeSync"))}">${enabled ? "⏸" : "▶"}</button>
          <button type="button" class="btn btn-outline btn-small" data-gd-action="edit" data-idx="${idx}" title="${esc(SyncApp.t("edit"))}">✏️</button>
          <button type="button" class="btn btn-danger btn-small" data-gd-action="delete" data-idx="${idx}" title="${esc(SyncApp.t("delete"))}">🗑️</button>
        </div>
      `;

      if (canDrag) {
        card.addEventListener("dragstart", (e) => {
          gdDragFrom = idx;
          card.classList.add("dragging");
          e.dataTransfer.effectAllowed = "move";
        });
        card.addEventListener("dragend", () => {
          card.classList.remove("dragging");
          gdDragFrom = null;
          list.querySelectorAll(".drag-over").forEach((n) => n.classList.remove("drag-over"));
        });
        card.addEventListener("dragover", (e) => { e.preventDefault(); card.classList.add("drag-over"); });
        card.addEventListener("dragleave", () => card.classList.remove("drag-over"));
        card.addEventListener("drop", async (e) => {
          e.preventDefault();
          card.classList.remove("drag-over");
          if (gdDragFrom === null || gdDragFrom === idx) return;
          const order = gdFolders.map((_, i) => i);
          const movedIdx = order.splice(gdDragFrom, 1)[0];
          order.splice(idx, 0, movedIdx);
          const movedObj = gdFolders.splice(gdDragFrom, 1)[0];
          gdFolders.splice(idx, 0, movedObj);
          gdDragFrom = null;
          renderGDList();
          const res = await SyncApp.api("/gdrive/reorder_folders", { method: "POST", body: { order } });
          if (res.ok && res.data.success) SyncApp.toast(SyncApp.t("gdOrderSaved"), "success");
          else SyncApp.toast(res.data.error || "Could not save order", "error");
        });
      }
      list.appendChild(card);
    });
  }

  async function saveGDForm(e) {
    e.preventDefault();
    const idx = parseInt(document.getElementById("gd-edit-index").value, 10);
    const payload = {
      name: document.getElementById("gd-name").value.trim(),
      folder_url: document.getElementById("gd-url").value.trim(),
      local_path: document.getElementById("gd-path").value.trim(),
    };
    const url = idx >= 0 ? "/gdrive/update_folder" : "/gdrive/add_folder";
    if (idx >= 0) payload.index = idx;

    const { ok, data } = await SyncApp.api(url, { method: "POST", body: payload });
    if (ok && data.success) {
      if (idx >= 0 && data.config) gdFolders[idx] = data.config;
      else if (data.config) gdFolders.push(data.config);
      renderGDList();
      closeGDModal();
      SyncApp.toast("Google Drive folder saved", "success");
    } else {
      SyncApp.toast(data.error || "Failed to save Google Drive folder", "error");
    }
  }

  const debouncedAutoFetchGDName = SyncApp.debounce(async () => {
    const url = (document.getElementById("gd-url")?.value || "").trim();
    const nameField = document.getElementById("gd-name");
    const pathField = document.getElementById("gd-path");
    if (!url || !nameField || nameField.value.trim() !== "") return;
    if (!url.includes("drive.google.com") && url.length < 15) return;
    nameField.placeholder = "Fetching folder name...";
    try {
      const { ok, data } = await SyncApp.api("/gdrive/get_folder_info", {
        method: "POST",
        body: { folder_url: url },
      });
      if (ok && data.success && data.name) {
        nameField.value = data.name;
        if (pathField && pathField.value.trim() && !pathField.value.trim().endsWith(data.name)) {
          const cur = pathField.value.trim();
          const sep = cur.includes("\\") ? "\\" : "/";
          pathField.value = cur.replace(/[\\/]+$/, "") + sep + data.name;
        }
      }
    } catch (_) {
    } finally {
      nameField.placeholder = "";
    }
  }, 500);

  async function triggerGdSync(opts = {}) {
    const { ok, data } = await SyncApp.api("/gdrive/sync", { method: "POST", body: opts });
    if (ok && data.success) {
      SyncApp.toast(opts.dry_run ? "🔍 Google Drive dry-run started..." : "🔄 Google Drive sync started", "success");
      SyncApp.pollLiveMetrics();
    } else {
      SyncApp.toast(data.error || "Could not start Google Drive sync", "error");
    }
  }

  SyncApp.folders = {
    reloadSPConfigs,
    renderSPList,
    renderGDList,
    switchSpView,
  };

  document.addEventListener("DOMContentLoaded", () => {
    // SharePoint sub-tabs
    document.querySelectorAll("[data-sp-view]").forEach((btn) => {
      btn.addEventListener("click", () => switchSpView(btn.getAttribute("data-sp-view")));
    });

    // SharePoint state filter pills
    document.querySelectorAll("#sp-state-filter [data-state]").forEach((btn) => {
      btn.addEventListener("click", () => {
        spStateFilter = btn.getAttribute("data-state") || "all";
        document.querySelectorAll("#sp-state-filter [data-state]").forEach((b) => b.classList.toggle("active", b === btn));
        renderSPList();
      });
    });

    // SharePoint modal & form
    document.getElementById("btn-sp-open-add")?.addEventListener("click", () => { resetSPForm(); openSPModal(); });
    document.getElementById("sp-modal-close")?.addEventListener("click", closeSPModal);
    document.getElementById("sp-cancel-btn")?.addEventListener("click", closeSPModal);
    document.getElementById("sp-form-modal")?.addEventListener("click", (e) => {
      if (e.target.id === "sp-form-modal") closeSPModal();
    });
    document.getElementById("sp-form")?.addEventListener("submit", saveSPForm);
    document.getElementById("sp-entry-type")?.addEventListener("change", (e) => {
      const w = document.getElementById("sp-local-flat-wrap");
      if (w) w.style.display = e.target.value === "file" ? "none" : "";
    });
    document.getElementById("btn-sp-browse-local")?.addEventListener("click", () => SyncApp.browseFolder("sp-path"));
    document.getElementById("btn-sp-sync")?.addEventListener("click", () => triggerSpSync({ dry_run: false }));
    document.getElementById("sp-filter-input")?.addEventListener("input", renderSPList);

    // SharePoint Bulk selection & actions
    document.getElementById("sp-select-all-visible")?.addEventListener("change", (e) => {
      const check = !!e.target.checked;
      document.querySelectorAll("#sp-list .sp-select-cb").forEach((cb) => {
        const idx = parseInt(cb.getAttribute("data-idx"), 10);
        cb.checked = check;
        if (check) spSelected.add(idx);
        else spSelected.delete(idx);
      });
      updateSpBulkBar();
    });
    document.getElementById("sp-list")?.addEventListener("change", (e) => {
      const cb = e.target.closest(".sp-select-cb");
      if (!cb) return;
      const idx = parseInt(cb.getAttribute("data-idx"), 10);
      if (cb.checked) spSelected.add(idx);
      else spSelected.delete(idx);
      updateSpBulkBar();
    });
    document.getElementById("btn-sp-bulk-enable")?.addEventListener("click", () => runSpBulkAction("enable"));
    document.getElementById("btn-sp-bulk-disable")?.addEventListener("click", () => runSpBulkAction("disable"));
    document.getElementById("btn-sp-bulk-sync")?.addEventListener("click", () => runSpBulkAction("sync"));
    document.getElementById("btn-sp-bulk-clear")?.addEventListener("click", () => {
      spSelected.clear();
      const selAll = document.getElementById("sp-select-all-visible");
      if (selAll) selAll.checked = false;
      renderSPList();
    });

    document.getElementById("sp-list")?.addEventListener("click", async (e) => {
      const btn = e.target.closest("[data-sp-action]");
      if (!btn) return;
      const action = btn.getAttribute("data-sp-action");
      const idx = parseInt(btn.getAttribute("data-idx"), 10);
      const item = spFolders[idx];
      if (!item) return;

      if (action === "sync") {
        await triggerSpSync({ index: idx, dry_run: false });
      } else if (action === "open") {
        await SyncApp.openLocalFolder(item.local_path);
      } else if (action === "toggle") {
        const res = await SyncApp.api("/sp/toggle_sync", {
          method: "POST",
          body: { index: idx, enabled: !spEnabled(item) },
        });
        if (res.ok && res.data.success) {
          spFolders[idx].sync_enabled = res.data.enabled;
          renderSPList();
          SyncApp.toast(SyncApp.t(res.data.enabled ? "spSyncEnabled" : "spSyncPaused"), "success");
        } else {
          SyncApp.toast(res.data.error || "Could not toggle", "error");
        }
      } else if (action === "edit") {
        editSP(idx);
      } else if (action === "delete") {
        const yes = await SyncApp.confirm(`Delete SharePoint target "${item.name}"?`);
        if (!yes) return;
        const res = await SyncApp.api("/sp/remove_folder", { method: "POST", body: { index: idx } });
        if (res.ok && res.data.success) {
          spFolders.splice(idx, 1);
          renderSPList();
          SyncApp.toast("Target deleted", "success");
        } else {
          SyncApp.toast(res.data.error || "Delete failed", "error");
        }
      }
    });

    // Google Drive state filter pills & modal
    document.querySelectorAll("#gd-state-filter [data-state]").forEach((btn) => {
      btn.addEventListener("click", () => {
        gdStateFilter = btn.getAttribute("data-state") || "all";
        document.querySelectorAll("#gd-state-filter [data-state]").forEach((b) => b.classList.toggle("active", b === btn));
        renderGDList();
      });
    });

    document.getElementById("btn-gd-open-add")?.addEventListener("click", () => { resetGDForm(); openGDModal(); });
    document.getElementById("gd-modal-close")?.addEventListener("click", closeGDModal);
    document.getElementById("gd-cancel-btn")?.addEventListener("click", closeGDModal);
    document.getElementById("gd-form-modal")?.addEventListener("click", (e) => {
      if (e.target.id === "gd-form-modal") closeGDModal();
    });
    document.getElementById("gd-form")?.addEventListener("submit", saveGDForm);
    document.getElementById("gd-url")?.addEventListener("input", debouncedAutoFetchGDName);
    document.getElementById("btn-gd-browse-local")?.addEventListener("click", () => SyncApp.browseFolder("gd-path"));
    document.getElementById("btn-gd-sync")?.addEventListener("click", () => triggerGdSync({ dry_run: false }));
    document.getElementById("gd-filter-input")?.addEventListener("input", renderGDList);

    document.getElementById("gd-list")?.addEventListener("click", async (e) => {
      const btn = e.target.closest("[data-gd-action]");
      if (!btn) return;
      const action = btn.getAttribute("data-gd-action");
      const idx = parseInt(btn.getAttribute("data-idx"), 10);
      const item = gdFolders[idx];
      if (!item) return;

      if (action === "sync") {
        await triggerGdSync({ index: idx, dry_run: false });
      } else if (action === "open") {
        await SyncApp.openLocalFolder(item.local_path);
      } else if (action === "toggle") {
        const res = await SyncApp.api("/gdrive/toggle_sync", {
          method: "POST",
          body: { index: idx, enabled: !gdEnabled(item) },
        });
        if (res.ok && res.data.success) {
          gdFolders[idx].sync_enabled = res.data.enabled;
          renderGDList();
          SyncApp.toast(SyncApp.t(res.data.enabled ? "spSyncEnabled" : "spSyncPaused"), "success");
        }
      } else if (action === "edit") {
        editGD(idx);
      } else if (action === "delete") {
        const yes = await SyncApp.confirm(`Delete Google Drive folder "${item.name}"?`);
        if (!yes) return;
        const res = await SyncApp.api("/gdrive/remove_folder", { method: "POST", body: { index: idx } });
        if (res.ok && res.data.success) {
          gdFolders.splice(idx, 1);
          renderGDList();
          SyncApp.toast("Folder deleted", "success");
        }
      }
    });

    window.addEventListener("app:langchange", () => {
      renderSPList();
      renderGDList();
    });

    renderSPList();
    renderGDList();
  });
})();
