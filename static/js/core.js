/**
 * Core Shared Shell Utilities — Mounted on every page via base.html
 * Includes: API wrapper, Toast stack, Confirm modal, Theme/Lang toggles,
 * Global Multi-Service Live Sync Progress Bar, Sync All / Dry Run / Stop, and GDrive OAuth helpers.
 */
(function () {
  const SyncApp = (window.SyncApp = window.SyncApp || {});

  SyncApp.t = (key, vars) => (window.APP_I18N ? window.APP_I18N.t(key, vars) : key);
  SyncApp.getLang = () => (window.APP_I18N ? window.APP_I18N.getLang() : "en");

  SyncApp.escapeHtml = function escapeHtml(s) {
    if (s == null) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  };

  SyncApp.formatBytes = function formatBytes(bytes) {
    const n = Number(bytes || 0);
    if (!Number.isFinite(n) || n <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    let b = n;
    while (b >= 1024 && i < units.length - 1) {
      b /= 1024;
      i++;
    }
    return `${b.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
  };

  SyncApp.formatEta = function formatEta(seconds) {
    if (seconds == null || seconds <= 0 || !Number.isFinite(seconds)) return "--";
    if (seconds < 60) return `${Math.round(seconds)}s`;
    const mins = Math.floor(seconds / 60);
    const secs = Math.round(seconds % 60);
    if (mins < 60) return `${mins}m ${secs}s`;
    const hrs = Math.floor(mins / 60);
    return `${hrs}h ${mins % 60}m`;
  };

  SyncApp.debounce = function debounce(fn, waitMs) {
    let timer = null;
    return function (...args) {
      clearTimeout(timer);
      timer = setTimeout(() => fn.apply(this, args), waitMs);
    };
  };

  // Global Toast Stack
  SyncApp.toast = function toast(msg, type = "success", durationMs = 4000) {
    let stack = document.getElementById("toast-stack");
    if (!stack) {
      stack = document.createElement("div");
      stack.id = "toast-stack";
      stack.className = "toast-stack";
      document.body.appendChild(stack);
    }
    const item = document.createElement("div");
    item.className = `toast-item ${type}`;
    item.textContent = String(msg || "");
    stack.appendChild(item);
    setTimeout(() => {
      item.style.opacity = "0";
      item.style.transform = "translateY(8px)";
      item.style.transition = "opacity 0.2s ease, transform 0.2s ease";
      setTimeout(() => item.remove(), 220);
    }, durationMs);
  };

  // Unified JSON API wrapper
  SyncApp.api = async function api(url, opts = {}) {
    const fetchOpts = Object.assign({ headers: {} }, opts);
    if (fetchOpts.body && typeof fetchOpts.body === "object" && !(fetchOpts.body instanceof FormData)) {
      fetchOpts.headers["Content-Type"] = "application/json";
      fetchOpts.body = JSON.stringify(fetchOpts.body);
    }
    const res = await fetch(url, fetchOpts);
    const text = await res.text();
    let data = {};
    try {
      data = text ? JSON.parse(text) : {};
    } catch (_) {
      data = { success: false, error: `Invalid server response (HTTP ${res.status})`, raw: text };
    }
    return { ok: res.ok, status: res.status, data };
  };

  // Non-blocking Confirmation Modal
  SyncApp.confirm = function confirmDialog(message, title) {
    return new Promise((resolve) => {
      const modal = document.getElementById("confirm-modal");
      const titleEl = document.getElementById("confirm-modal-title");
      const msgEl = document.getElementById("confirm-modal-msg");
      const okBtn = document.getElementById("confirm-modal-ok");
      const cancelBtn = document.getElementById("confirm-modal-cancel");
      if (!modal || !okBtn || !cancelBtn) {
        resolve(window.confirm(message));
        return;
      }
      titleEl.textContent = title || SyncApp.t("confirm");
      msgEl.textContent = message;
      modal.classList.add("open");

      function cleanup(val) {
        modal.classList.remove("open");
        okBtn.removeEventListener("click", onOk);
        cancelBtn.removeEventListener("click", onCancel);
        modal.removeEventListener("click", onBackdrop);
        resolve(val);
      }
      function onOk() { cleanup(true); }
      function onCancel() { cleanup(false); }
      function onBackdrop(e) { if (e.target === modal) cleanup(false); }

      okBtn.addEventListener("click", onOk);
      cancelBtn.addEventListener("click", onCancel);
      modal.addEventListener("click", onBackdrop);
    });
  };

  SyncApp.browseFolder = async function browseFolder(inputId) {
    try {
      const { data } = await SyncApp.api("/api/browse_folder");
      if (data && data.path) {
        const el = document.getElementById(inputId);
        if (el) el.value = data.path;
      } else if (data && data.error) {
        SyncApp.toast(data.error, "warning");
      }
    } catch (_) {
      SyncApp.toast("Could not open folder picker", "error");
    }
  };

  SyncApp.openLocalFolder = async function openLocalFolder(folderPath) {
    if (!folderPath) {
      SyncApp.toast("No path specified", "error");
      return;
    }
    try {
      const { ok, data } = await SyncApp.api("/api/open_local_folder", {
        method: "POST",
        body: { folder_path: folderPath },
      });
      if (ok && data.success) {
        SyncApp.toast("Opening folder...", "success", 2200);
      } else {
        SyncApp.toast(data.error || "Could not open folder", "error");
      }
    } catch (_) {
      SyncApp.toast("Error opening folder", "error");
    }
  };

  // Theme management
  SyncApp.applyTheme = function applyTheme(theme) {
    const val = theme === "light" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", val);
    localStorage.setItem("ui_theme", val);
    const btn = document.getElementById("btn-theme-toggle");
    if (btn) {
      btn.textContent = val === "dark" ? "☀️ " + SyncApp.t("themeLight") : "🌙 " + SyncApp.t("themeDark");
    }
  };

  // --- Shared Google Drive OAuth Helpers (usable from Settings or GDrive page) ---
  let gdPollTimer = null;
  SyncApp.startGDAuth = async function startGDAuth() {
    const liveBox = document.getElementById("gd-auth-live");
    if (liveBox) liveBox.style.display = "block";
    const { ok, data } = await SyncApp.api("/gdrive/auth/start", { method: "POST" });
    if (ok && data.success && data.auth_url) {
      const link = document.getElementById("gd-auth-direct");
      if (link) link.href = data.auth_url;
      window.open(data.auth_url, "_blank");
      SyncApp.toast("Google sign-in tab opened", "success");
      if (gdPollTimer) clearInterval(gdPollTimer);
      gdPollTimer = setInterval(async () => {
        const stRes = await SyncApp.api("/gdrive/auth/status");
        if (stRes.ok && (stRes.data.authenticated || stRes.data.flow_status === "success")) {
          clearInterval(gdPollTimer);
          SyncApp.toast("✅ Google Drive connected!", "success");
          setTimeout(() => window.location.reload(), 800);
        } else if (stRes.ok && stRes.data.flow_status === "error") {
          clearInterval(gdPollTimer);
          SyncApp.toast("Auth failed: " + (stRes.data.error || "Unknown error"), "error");
          if (liveBox) liveBox.style.display = "none";
        }
      }, 2000);
    } else {
      SyncApp.toast(data.error || "Could not start Google sign-in", "error");
      if (liveBox) liveBox.style.display = "none";
    }
  };

  SyncApp.disconnectGD = async function disconnectGD() {
    const yes = await SyncApp.confirm("Disconnect Google Drive account?");
    if (!yes) return;
    const { ok } = await SyncApp.api("/gdrive/auth/disconnect", { method: "POST" });
    if (ok) {
      SyncApp.toast("Google Drive disconnected", "success");
      setTimeout(() => window.location.reload(), 600);
    }
  };

  // --- Global Multi-Service Live Progress Bar (runs on EVERY page!) ---
  let wasSyncActive = false;
  let metricsTimer = null;

  function renderServiceProgressBlock(label, svcKey, s) {
    const esc = SyncApp.escapeHtml;
    const pct = Math.min(100, Math.max(0, Number(s.percent || 0)));
    const fileLabel = s.current_file || s.status_message || "Preparing files...";
    const folderPrefix = s.current_folder ? `[${s.current_folder}] ` : "";
    const staleHtml =
      s.status_state === "stalling"
        ? `<span class="badge badge-warn">⚠️ Stalled ${s.stale_seconds}s</span>`
        : s.status_state === "stopping"
        ? `<span class="badge badge-err">🛑 Stopping...</span>`
        : `<span class="badge badge-accent">${pct}%</span>`;
    const queueText = s.total_files > 0 ? `${s.files_completed} / ${s.total_files}` : "";

    return `
      <div class="live-service-block">
        <div class="card-header-row" style="margin-bottom:6px;">
          <div style="display:flex;align-items:center;gap:8px;min-width:0;">
            <span class="pulse-dot"></span>
            <strong>${esc(label)}</strong>
            ${staleHtml}
          </div>
          <div style="display:flex;align-items:center;gap:8px;">
            ${queueText ? `<span class="muted">${esc(queueText)}</span>` : ""}
            <a href="/logs" class="btn btn-outline btn-small">📝 <span data-i18n="navLogs">${esc(SyncApp.t("navLogs"))}</span></a>
            <button type="button" class="btn btn-danger btn-small" data-stop-service="${esc(svcKey)}" ${s.stopping ? "disabled" : ""}>
              🛑 ${esc(SyncApp.t("btnStopSync"))}
            </button>
          </div>
        </div>
        <div style="font-size:0.9rem;font-weight:600;word-break:break-all;">${esc(folderPrefix + fileLabel)}</div>
        <div class="live-progress-bar-bg">
          <div class="live-progress-bar-fill" style="width:${pct}%;"></div>
        </div>
        <div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;font-size:0.82rem;" class="muted">
          <span>${esc(SyncApp.formatBytes(s.bytes_transferred))} / ${esc(SyncApp.formatBytes(s.total_bytes))} (${pct.toFixed(0)}%)</span>
          <span style="color:var(--accent);font-weight:600;">${Number(s.speed_mb_s || 0).toFixed(2)} MB/s • ETA: ${esc(SyncApp.formatEta(s.eta_seconds))}</span>
        </div>
      </div>
    `;
  }

  function scheduleNextMetricsPoll(ms) {
    clearTimeout(metricsTimer);
    metricsTimer = setTimeout(SyncApp.pollLiveMetrics, ms);
  }

  SyncApp.pollLiveMetrics = async function pollLiveMetrics() {
    if (document.hidden) {
      scheduleNextMetricsPoll(4000);
      return;
    }
    try {
      const { ok, data } = await SyncApp.api("/sync/live_metrics");
      if (!ok || !data) {
        scheduleNextMetricsPoll(5000);
        return;
      }

      const card = document.getElementById("live-progress-card");
      const body = document.getElementById("live-progress-body");
      const stopAllBtn = document.getElementById("btn-stop-all");
      const logLiveDot = document.getElementById("nav-log-live-dot");

      const svcs = data.services || {};
      const activeBlocks = [];
      if (svcs.sharepoint && svcs.sharepoint.is_active) {
        activeBlocks.push(renderServiceProgressBlock("SharePoint", "sharepoint", svcs.sharepoint));
      }
      if (svcs.gdrive && svcs.gdrive.is_active) {
        activeBlocks.push(renderServiceProgressBlock("Google Drive", "gdrive", svcs.gdrive));
      }

      const active = Boolean(data.is_active || activeBlocks.length > 0);
      if (logLiveDot) logLiveDot.classList.toggle("active", active);

      if (active && card && body) {
        if (!activeBlocks.length) {
          activeBlocks.push(renderServiceProgressBlock(data.service || "Sync", data.service || "all", data));
        }
        body.innerHTML = activeBlocks.join("");
        card.classList.add("visible");
        card.classList.toggle("state-stalling", data.status_state === "stalling");
        card.classList.toggle("state-stopping", data.status_state === "stopping");
        if (stopAllBtn) stopAllBtn.style.display = "inline-flex";
        wasSyncActive = true;
        scheduleNextMetricsPoll(1200);
      } else {
        if (card) card.classList.remove("visible", "state-stalling", "state-stopping");
        if (stopAllBtn) stopAllBtn.style.display = "none";
        if (wasSyncActive) {
          wasSyncActive = false;
          if ("Notification" in window && Notification.permission === "granted") {
            try { new Notification("Unified Sync Tool", { body: "Synchronization finished!" }); } catch (_) {}
          }
          SyncApp.toast("🎉 Synchronization finished!", "success");
          window.dispatchEvent(new CustomEvent("app:syncfinished"));
        }
        scheduleNextMetricsPoll(4500);
      }
    } catch (_) {
      scheduleNextMetricsPoll(6000);
    }
  };

  SyncApp.triggerSyncAll = async function triggerSyncAll(dryRun = false) {
    const btn = document.getElementById(dryRun ? "btn-dry-run-all" : "btn-sync-all");
    if (btn) btn.disabled = true;
    try {
      const { ok, data } = await SyncApp.api("/sync/all", {
        method: "POST",
        body: { dry_run: !!dryRun },
      });
      if (ok && data.success) {
        SyncApp.toast(dryRun ? "🔍 Dry-run preview started..." : "🔄 Sync started in background", "success");
        SyncApp.pollLiveMetrics();
      } else {
        SyncApp.toast(data.error || "Could not start sync", "error");
      }
    } catch (_) {
      SyncApp.toast("Could not reach server", "error");
    } finally {
      setTimeout(() => { if (btn) btn.disabled = false; }, 1600);
    }
  };

  SyncApp.requestStopSync = async function requestStopSync(service = "all") {
    try {
      const { ok, data } = await SyncApp.api("/sync/stop", {
        method: "POST",
        body: { service },
      });
      if (ok && data.success) {
        SyncApp.toast("🛑 Stop requested — cancelling cleanly...", "warning");
        SyncApp.pollLiveMetrics();
      } else {
        SyncApp.toast(data.error || "Could not stop sync", "error");
      }
    } catch (_) {
      SyncApp.toast("Could not reach server", "error");
    }
  };

  document.addEventListener("DOMContentLoaded", () => {
    const savedTheme = localStorage.getItem("ui_theme") || "dark";
    SyncApp.applyTheme(savedTheme);

    document.getElementById("btn-theme-toggle")?.addEventListener("click", () => {
      const cur = document.documentElement.getAttribute("data-theme") || "dark";
      SyncApp.applyTheme(cur === "dark" ? "light" : "dark");
    });

    window.addEventListener("app:langchange", () => {
      const cur = document.documentElement.getAttribute("data-theme") || "dark";
      SyncApp.applyTheme(cur);
    });

    // Global Sync All / Dry Run / Stop buttons in Topbar
    document.getElementById("btn-sync-all")?.addEventListener("click", () => SyncApp.triggerSyncAll(false));
    document.getElementById("btn-dry-run-all")?.addEventListener("click", () => SyncApp.triggerSyncAll(true));
    document.getElementById("btn-stop-all")?.addEventListener("click", () => SyncApp.requestStopSync("all"));
    document.getElementById("live-progress-body")?.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-stop-service]");
      if (btn) SyncApp.requestStopSync(btn.getAttribute("data-stop-service") || "all");
    });

    // Shared GDrive auth buttons
    document.querySelectorAll("[data-gd-auth-start]").forEach((b) => b.addEventListener("click", SyncApp.startGDAuth));
    document.querySelectorAll("[data-gd-auth-disconnect]").forEach((b) => b.addEventListener("click", SyncApp.disconnectGD));

    // Mobile sidebar drawer
    const menuBtn = document.getElementById("btn-menu-toggle");
    const sb = document.getElementById("sidebar");
    const bd = document.getElementById("sidebar-backdrop");
    if (menuBtn && sb && bd) {
      menuBtn.addEventListener("click", () => {
        sb.classList.toggle("open");
        bd.classList.toggle("open");
      });
      bd.addEventListener("click", () => {
        sb.classList.remove("open");
        bd.classList.remove("open");
      });
    }

    // Escape closes any open modal
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        document.querySelectorAll(".modal-backdrop.open").forEach((m) => m.classList.remove("open"));
      }
    });

    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) SyncApp.pollLiveMetrics();
    });

    SyncApp.pollLiveMetrics();
  });
})();
