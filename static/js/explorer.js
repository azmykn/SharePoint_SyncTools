/**
 * SharePoint Explorer — Unified i18n, FIXED Back navigation (parent item_id), Clickable Breadcrumbs.
 */
(function () {
  const SyncApp = window.SyncApp;
  const esc = SyncApp.escapeHtml;
  const LOCAL_ROOT_KEY = "spx_local_root_v1";

  let historyStack = []; // each entry: { rel_url, name, parent_item_id }
  let sessionPrimeUrl = "";
  let sessionSharingUrl = "";
  let sessionShareBrowseRoot = "";
  let sessionParentItemId = "";
  let lastBrowseData = null;

  function pickApiText(data, baseKey) {
    if (!data) return "";
    const lang = SyncApp.getLang();
    if (lang === "ar" && data[baseKey + "_ar"]) return String(data[baseKey + "_ar"]);
    if (data[baseKey + "_en"]) return String(data[baseKey + "_en"]);
    if (data[baseKey]) return String(data[baseKey]);
    return "";
  }

  function joinLocalPath(root, name) {
    const r = (root || "").trim();
    if (!r) return name;
    const sep = r.includes("\\") ? "\\" : "/";
    return r.replace(/[\\/]+$/, "") + sep + name;
  }

  function renderBreadcrumbs() {
    const bc = document.getElementById("spx-bc");
    if (!bc) return;
    bc.innerHTML = "";
    if (!historyStack.length) return;

    historyStack.forEach((entry, i) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "breadcrumb-btn";
      btn.textContent = entry.name || entry.rel_url;
      btn.addEventListener("click", () => {
        const target = historyStack[i];
        historyStack = historyStack.slice(0, i);
        sessionParentItemId = target.parent_item_id || "";
        document.getElementById("spx-parent").value = target.rel_url;
        exploreRun();
      });
      bc.appendChild(btn);
      const sep = document.createElement("span");
      sep.textContent = "/";
      bc.appendChild(sep);
    });
  }

  function showLoading() {
    const wrap = document.getElementById("spx-results");
    if (wrap) wrap.innerHTML = `<div class="empty-state">⏳ ${esc(SyncApp.t("expLoading"))}</div>`;
  }

  function showBrowseError(title, status, data) {
    const wrap = document.getElementById("spx-results");
    if (!wrap) return;
    const perm = pickApiText(data, "permission_hint");
    const snip = data && data.body_snippet ? String(data.body_snippet).slice(0, 600) : "";
    const meta = [
      data && data.method ? `method=${data.method}` : "",
      data && data.content_type ? `content-type=${data.content_type}` : "",
    ]
      .filter(Boolean)
      .join(" • ");

    wrap.innerHTML = `
      <div class="error-panel">
        <div style="font-weight:700;margin-bottom:6px;">❌ ${esc(title)}${status ? ` (HTTP ${esc(status)})` : ""}</div>
        ${perm ? `<div style="margin-bottom:6px;">${esc(perm)}</div>` : ""}
        ${snip ? `<div class="muted" style="font-family:monospace;font-size:0.78rem;">${esc(snip)}</div>` : ""}
        ${meta ? `<div class="muted" style="margin-top:6px;font-size:0.76rem;">${esc(meta)}</div>` : ""}
      </div>
    `;
  }

  function renderTable(data) {
    if (!data || data.success === false) return;
    lastBrowseData = data;
    const wrap = document.getElementById("spx-results");
    if (!wrap) return;

    const root = (document.getElementById("spx-local-root")?.value || "").trim();
    const filterQ = (document.getElementById("spx-filter")?.value || "").trim().toLowerCase();
    const folders = (data.folders || []).filter((f) => !filterQ || (f.name || "").toLowerCase().includes(filterQ));
    const files = (data.files || []).filter((f) => !filterQ || (f.name || "").toLowerCase().includes(filterQ));

    if (!folders.length && !files.length) {
      const hint = pickApiText(data, "hint");
      wrap.innerHTML = `
        <div class="empty-state">
          <div>${esc(SyncApp.t("expEmpty"))}</div>
          ${hint ? `<div class="muted" style="margin-top:8px;">${esc(hint)}</div>` : ""}
        </div>
      `;
      return;
    }

    const totalCount = folders.length + files.length;
    wrap.innerHTML = `
      <div class="toolbar-row" style="margin-bottom:10px;">
        <button type="button" class="btn btn-outline btn-small" id="spx-sel-all">${esc(SyncApp.t("expSelectAll"))}</button>
        <button type="button" class="btn btn-outline btn-small" id="spx-sel-none">${esc(SyncApp.t("expClearSel"))}</button>
        <span class="muted">${totalCount} item(s) • method: <code>${esc(data.browse_method || "rest")}</code></span>
      </div>
      <div class="table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              <th style="width:36px;"></th>
              <th>${esc(SyncApp.t("expColType"))}</th>
              <th>${esc(SyncApp.t("expColName"))}</th>
              <th>${esc(SyncApp.t("expColSize"))}</th>
              <th>${esc(SyncApp.t("expColLocal"))}</th>
              <th>${esc(SyncApp.t("expColAction"))}</th>
            </tr>
          </thead>
          <tbody id="spx-tbody"></tbody>
        </table>
      </div>
    `;

    const tbody = document.getElementById("spx-tbody");
    folders.forEach((fo) => {
      const tr = document.createElement("tr");
      tr.className = "ex-row-folder";
      tr.dataset.relUrl = fo.rel_url;
      tr.dataset.name = fo.name;
      tr.dataset.itemId = fo.item_id || "";
      tr.innerHTML = `
        <td><input type="checkbox" class="ex-pick ex-folder-check"></td>
        <td><span class="badge badge-accent">📁 ${esc(SyncApp.t("expTypeFolder"))}</span></td>
        <td><strong>${esc(fo.name)}</strong></td>
        <td class="muted">—</td>
        <td><input type="text" class="form-input ex-local-path" dir="ltr" value="${esc(joinLocalPath(root, fo.name))}"></td>
        <td><button type="button" class="btn btn-outline btn-small ex-open-folder">${esc(SyncApp.t("expBtnOpen"))}</button></td>
      `;
      tr.querySelector(".ex-open-folder").addEventListener("click", () => {
        const curPath = (document.getElementById("spx-parent")?.value || "").trim();
        const curLeaf = curPath.split("/").filter(Boolean).pop() || curPath;
        // CRITICAL FIX: save CURRENT parent's item_id (NOT child's fo.item_id) so Back works properly!
        historyStack.push({
          rel_url: curPath,
          name: curLeaf,
          parent_item_id: sessionParentItemId,
        });
        sessionParentItemId = fo.item_id || "";
        document.getElementById("spx-parent").value = fo.rel_url;
        exploreRun();
      });
      tbody.appendChild(tr);
    });

    files.forEach((fi) => {
      const tr = document.createElement("tr");
      tr.className = "ex-row-file";
      tr.dataset.relUrl = fi.rel_url;
      tr.dataset.name = fi.name;
      tr.innerHTML = `
        <td><input type="checkbox" class="ex-pick ex-file-check"></td>
        <td><span class="badge">📄 ${esc(SyncApp.t("expTypeFile"))}</span></td>
        <td>${esc(fi.name)}</td>
        <td class="muted">${fi.size != null ? esc(SyncApp.formatBytes(fi.size)) : "—"}</td>
        <td><input type="text" class="form-input ex-local-path" dir="ltr" value="${esc(joinLocalPath(root, fi.name))}"></td>
        <td></td>
      `;
      tbody.appendChild(tr);
    });

    document.getElementById("spx-sel-all")?.addEventListener("click", () => {
      wrap.querySelectorAll(".ex-pick").forEach((cb) => { cb.checked = true; });
    });
    document.getElementById("spx-sel-none")?.addEventListener("click", () => {
      wrap.querySelectorAll(".ex-pick").forEach((cb) => { cb.checked = false; });
    });
  }

  async function exploreRun() {
    const parentInput = document.getElementById("spx-parent");
    if (!parentInput) return;
    let parent = (parentInput.value || "").trim();
    if (!parent) {
      SyncApp.toast("Enter or resolve a SharePoint path first", "warning");
      return;
    }
    if (!parent.startsWith("/")) parent = "/" + parent;
    parentInput.value = parent;
    renderBreadcrumbs();

    const payload = { parent_rel_url: parent };
    if (sessionParentItemId) payload.parent_item_id = sessionParentItemId;
    if (sessionShareBrowseRoot) payload.share_browse_root = sessionShareBrowseRoot;
    let prime = (sessionPrimeUrl || "").trim();
    if (!prime) {
      const pasted = (document.getElementById("spx-paste")?.value || "").trim();
      if (pasted.toLowerCase().startsWith("http") && pasted.toLowerCase().includes("sharepoint")) {
        prime = pasted;
      }
    }
    if (prime) payload.prime_url = prime;
    if (sessionSharingUrl) payload.sharing_link_url = sessionSharingUrl;

    showLoading();
    try {
      const { ok, status, data } = await SyncApp.api("/sp/browse", { method: "POST", body: payload });
      if (!ok || !data.success) {
        showBrowseError(data.error || "Browse failed", status, data);
        SyncApp.toast(data.error || "Browse failed", "error");
        lastBrowseData = null;
        return;
      }
      if (data.parent) parentInput.value = data.parent;
      renderTable(data);
      const nf = (data.folders || []).length;
      const nfi = (data.files || []).length;
      SyncApp.toast(`Loaded ${nf} folder(s), ${nfi} file(s)`, "success", 2500);
    } catch (e) {
      showBrowseError("Network error", "", { body_snippet: String(e) });
      SyncApp.toast("Connection error", "error");
      lastBrowseData = null;
    }
  }

  async function resolveLink() {
    const raw = (document.getElementById("spx-paste")?.value || "").trim();
    if (!raw) {
      SyncApp.toast("Paste a link or path first", "warning");
      return;
    }
    const meta = document.getElementById("spx-resolve-meta");
    if (meta) {
      meta.style.display = "block";
      meta.textContent = "⏳ Resolving link...";
    }
    try {
      const { ok, data } = await SyncApp.api("/sp/resolve_url", { method: "POST", body: { url: raw } });
      if (!ok || !data.success) {
        const msg = (data.error || "Could not resolve link") + (data.hint ? " — " + data.hint : "");
        if (meta) meta.textContent = "❌ " + msg;
        SyncApp.toast(data.error || "Could not resolve link", "error");
        return;
      }
      document.getElementById("spx-parent").value = data.browse_parent || "";
      historyStack = [];
      sessionPrimeUrl = (data.final_url || "").trim();
      sessionSharingUrl = (data.sharing_url || "").trim();
      if (!sessionSharingUrl && /:[fwx]:\//i.test(raw)) sessionSharingUrl = raw;
      sessionShareBrowseRoot = (data.sharing_root || data.browse_parent || "").trim();
      sessionParentItemId = "";
      if (meta) meta.textContent = `✅ Resolved (${data.resolved_from || "ok"}): ${data.browse_parent || ""}`;
      SyncApp.toast("Link resolved — loading folder", "success", 2200);
      exploreRun();
    } catch (_) {
      if (meta) meta.textContent = "❌ Connection error";
      SyncApp.toast("Connection error", "error");
    }
  }

  async function bulkAddSelected() {
    const items = [];
    document.querySelectorAll("#sp-view-explorer .ex-row-folder").forEach((row) => {
      if (!row.querySelector(".ex-folder-check")?.checked) return;
      const local_path = (row.querySelector(".ex-local-path")?.value || "").trim();
      if (row.dataset.relUrl && local_path) {
        items.push({ name: row.dataset.name, rel_url: row.dataset.relUrl, local_path, entry_type: "folder" });
      }
    });
    document.querySelectorAll("#sp-view-explorer .ex-row-file").forEach((row) => {
      if (!row.querySelector(".ex-file-check")?.checked) return;
      const local_path = (row.querySelector(".ex-local-path")?.value || "").trim();
      if (row.dataset.relUrl && local_path) {
        items.push({ name: row.dataset.name, rel_url: row.dataset.relUrl, local_path, entry_type: "file" });
      }
    });
    if (!items.length) {
      SyncApp.toast("Select at least one row with a local path", "warning");
      return;
    }
    if (sessionSharingUrl) {
      items.forEach((it) => {
        it.sharing_link = sessionSharingUrl;
        if (sessionShareBrowseRoot) it.sharing_root = sessionShareBrowseRoot;
      });
    }
    const { ok, data } = await SyncApp.api("/sp/bulk_add", { method: "POST", body: { items } });
    if (!ok || !data.success) {
      SyncApp.toast(data.error || "Failed to add targets", "error");
      return;
    }
    SyncApp.toast(`✅ Added/updated ${data.merged} target(s)`, "success");
    await SyncApp.folders?.reloadSPConfigs();
    SyncApp.folders?.switchSpView("targets");
  }

  document.addEventListener("DOMContentLoaded", () => {
    const rootInput = document.getElementById("spx-local-root");
    if (rootInput) {
      const savedRoot = localStorage.getItem(LOCAL_ROOT_KEY) || "";
      if (savedRoot && !rootInput.value) rootInput.value = savedRoot;
      rootInput.addEventListener("change", () => {
        localStorage.setItem(LOCAL_ROOT_KEY, rootInput.value.trim());
        if (lastBrowseData) renderTable(lastBrowseData);
      });
    }

    document.getElementById("spx-resolve")?.addEventListener("click", resolveLink);
    document.getElementById("spx-browse")?.addEventListener("click", exploreRun);
    document.getElementById("spx-bulk")?.addEventListener("click", bulkAddSelected);
    document.getElementById("btn-spx-browse-local")?.addEventListener("click", () => SyncApp.browseFolder("spx-local-root"));
    document.getElementById("spx-filter")?.addEventListener("input", () => {
      if (lastBrowseData) renderTable(lastBrowseData);
    });
    document.getElementById("spx-back")?.addEventListener("click", () => {
      if (!historyStack.length) return;
      const prev = historyStack.pop();
      sessionParentItemId = prev.parent_item_id || "";
      document.getElementById("spx-parent").value = prev.rel_url;
      exploreRun();
    });

    window.addEventListener("app:langchange", () => {
      if (lastBrowseData) renderTable(lastBrowseData);
    });
  });
})();
