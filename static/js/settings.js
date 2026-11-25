/**
 * Settings, Live Cookie Testing, Full Daily/Interval Scheduler, Video Processing, Health & Backup
 */
(function () {
  const SyncApp = window.SyncApp;
  const esc = SyncApp.escapeHtml;

  // --- Scheduler ---
  function toggleSchedulerModeFields() {
    const mode = document.getElementById("scheduler-mode")?.value || "daily";
    const dailyWrap = document.getElementById("scheduler-daily-wrap");
    const intervalWrap = document.getElementById("scheduler-interval-wrap");
    if (dailyWrap) dailyWrap.style.display = mode === "daily" ? "" : "none";
    if (intervalWrap) intervalWrap.style.display = mode === "interval" ? "" : "none";
  }

  function updateSchedulerUi(payload) {
    if (!payload || !payload.settings) return;
    const st = payload.settings;
    const en = document.getElementById("scheduler-enabled");
    const mode = document.getElementById("scheduler-mode");
    const dTime = document.getElementById("scheduler-daily-time");
    const intv = document.getElementById("scheduler-interval");
    const tgt = document.getElementById("scheduler-target");

    if (en) en.checked = !!st.enabled;
    if (mode && (st.schedule_mode === "daily" || st.schedule_mode === "interval")) mode.value = st.schedule_mode;
    if (dTime && st.daily_time) dTime.value = st.daily_time;
    if (intv && st.interval_hours) intv.value = String(st.interval_hours);
    if (tgt && st.target) tgt.value = st.target;
    toggleSchedulerModeFields();

    const lastEl = document.getElementById("scheduler-last-run");
    if (lastEl) {
      lastEl.textContent = st.last_run ? `Last run: ${st.last_run}` : "No scheduled runs recorded yet.";
    }
    const daemonChip = document.getElementById("scheduler-daemon-chip");
    if (daemonChip) {
      daemonChip.className = `status-chip ${payload.daemon_alive ? "ok" : "err"}`;
      daemonChip.textContent = `Daemon: ${payload.daemon_alive ? "Alive" : "Stopped"}`;
    }
    const dueChip = document.getElementById("scheduler-due-chip");
    if (dueChip) {
      dueChip.className = `status-chip ${payload.is_due ? "warn" : "ok"}`;
      dueChip.textContent = `Due now: ${payload.is_due ? "Yes" : "No"}`;
    }
    const nextEl = document.getElementById("scheduler-next-run");
    if (nextEl) nextEl.textContent = `Next run: ${st.next_run || "--"}`;
  }

  async function refreshSchedulerHealth() {
    const { ok, data } = await SyncApp.api("/scheduler/settings");
    if (ok && data.success) updateSchedulerUi(data);
  }

  async function saveScheduler(e) {
    e.preventDefault();
    const payload = {
      enabled: !!document.getElementById("scheduler-enabled")?.checked,
      schedule_mode: document.getElementById("scheduler-mode")?.value || "daily",
      daily_time: document.getElementById("scheduler-daily-time")?.value || "02:00",
      interval_hours: parseInt(document.getElementById("scheduler-interval")?.value || "6", 10),
      target: document.getElementById("scheduler-target")?.value || "all",
    };
    const { ok, data } = await SyncApp.api("/scheduler/settings", { method: "POST", body: payload });
    if (ok && data.success) {
      updateSchedulerUi(data);
      SyncApp.toast("✅ Scheduler settings saved", "success");
    } else {
      SyncApp.toast(data.error || "Failed to save scheduler", "error");
    }
  }

  async function runSchedulerNow() {
    const target = document.getElementById("scheduler-target")?.value || "all";
    const { ok, data } = await SyncApp.api("/scheduler/run_now", { method: "POST", body: { target } });
    if (ok && data.success) {
      SyncApp.toast(`▶️ Scheduled run triggered (${data.target})`, "success");
      refreshSchedulerHealth();
      SyncApp.pollLiveMetrics();
    } else {
      SyncApp.toast(data.error || "Scheduler run-now failed", "error");
    }
  }

  // --- System Health ---
  async function refreshSystemHealth() {
    const grid = document.getElementById("health-grid");
    if (!grid) return;
    const { ok, data } = await SyncApp.api("/api/system/health");
    if (!ok || !data.success || !data.health) return;
    const h = data.health;
    grid.innerHTML = `
      <div class="health-box">
        <div class="health-box-title">
          <span>🎬 ffmpeg / ffprobe</span>
          <span class="badge ${h.ffmpeg.available && h.ffprobe.available ? "badge-ok" : "badge-warn"}">
            ${h.ffmpeg.available ? "OK" : "Missing"}
          </span>
        </div>
        <div class="muted" style="word-break:break-all;">${esc(h.ffmpeg.path || "Not found on PATH")}</div>
      </div>
      <div class="health-box">
        <div class="health-box-title">
          <span>🌐 Playwright / Chromium</span>
          <span class="badge ${h.playwright.installed ? "badge-ok" : "badge-warn"}">
            ${h.playwright.installed ? "Installed" : "Missing"}
          </span>
        </div>
        <div class="muted" style="word-break:break-all;">${esc(h.playwright.system_chromium || "Bundled / default")}</div>
      </div>
      <div class="health-box">
        <div class="health-box-title">
          <span>💾 Disk & SQLite</span>
          <span class="badge ${h.disk.low_space ? "badge-err" : "badge-ok"}">${h.disk.free_gb} GB free</span>
        </div>
        <div class="muted">DB: ${h.database.size_kb} KB / Total disk: ${h.disk.total_gb} GB</div>
      </div>
      <div class="health-box">
        <div class="health-box-title">
          <span>🗂️ Temp Segments</span>
          <span class="badge ${h.tmp_segments.size_mb > 100 ? "badge-warn" : "badge-ok"}">${h.tmp_segments.size_mb} MB</span>
        </div>
        <div class="muted">${h.tmp_segments.dirs} dir(s), ${h.tmp_segments.files} file(s)</div>
      </div>
    `;
  }

  // --- Processing Settings ---
  async function saveAppSettings(e) {
    e.preventDefault();
    const payload = {
      trim_silence_enabled: !!document.getElementById("cfg-trim-enabled")?.checked,
      trim_silence_keep_backup: !!document.getElementById("cfg-trim-keep-backup")?.checked,
      trim_silence_noise_db: parseFloat(document.getElementById("cfg-trim-noise")?.value || "-70"),
      trim_silence_min_duration: parseFloat(document.getElementById("cfg-trim-min-dur")?.value || "2"),
      trim_silence_margin_sec: parseFloat(document.getElementById("cfg-trim-margin")?.value || "3"),
      trim_silence_merge_gap_sec: parseFloat(document.getElementById("cfg-trim-gap")?.value || "120"),
      duration_tolerance_pct: parseFloat(document.getElementById("cfg-dur-pct")?.value || "2") / 100.0,
      duration_tolerance_seconds: parseFloat(document.getElementById("cfg-dur-sec")?.value || "30"),
    };
    const { ok, data } = await SyncApp.api("/api/app_settings", { method: "POST", body: payload });
    if (ok && data.success) SyncApp.toast("✅ Processing settings saved", "success");
    else SyncApp.toast(data.error || "Could not save settings", "error");
  }

  // --- Desktop Notifications ---
  function initNotifyUi() {
    const statusEl = document.getElementById("notify-status-text");
    const btn = document.getElementById("btn-request-notify");
    if (!("Notification" in window)) {
      if (statusEl) statusEl.textContent = "Not supported in this browser";
      if (btn) btn.style.display = "none";
      return;
    }
    if (Notification.permission === "granted") {
      if (statusEl) statusEl.textContent = "✅ Enabled";
      if (btn) btn.disabled = true;
    } else if (Notification.permission === "denied") {
      if (statusEl) statusEl.textContent = "❌ Blocked in browser settings";
    } else {
      if (statusEl) statusEl.textContent = "Not enabled yet";
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    // SharePoint Site URL
    document.getElementById("form-sp-site-url")?.addEventListener("submit", async (e) => {
      e.preventDefault();
      const site_url = (document.getElementById("sp-site-url-manual")?.value || "").trim();
      const { ok, data } = await SyncApp.api("/sp/save_site_url", { method: "POST", body: { site_url } });
      if (ok && data.success) {
        SyncApp.toast("✅ SharePoint site URL saved", "success");
        setTimeout(() => window.location.reload(), 700);
      } else {
        SyncApp.toast(data.error || "Invalid site URL", "error");
      }
    });

    // SharePoint Guest Link (safe opt-in apply_to_all)
    document.getElementById("form-sp-guest-link")?.addEventListener("submit", async (e) => {
      e.preventDefault();
      const guest_link = (document.getElementById("sp-guest-link")?.value || "").trim();
      const apply_to_all = !!document.getElementById("sp-guest-apply-all")?.checked;
      const { ok, data } = await SyncApp.api("/sp/save_guest_link", {
        method: "POST",
        body: { guest_link, apply_to_all },
      });
      if (ok && data.success) {
        const extra = data.updated_folders ? ` (${data.updated_folders} folder link(s) updated)` : "";
        SyncApp.toast((guest_link ? "Guest link saved" : "Guest link cleared") + extra, "success");
      } else {
        SyncApp.toast(data.error || "Save failed", "error");
      }
    });

    // SharePoint Cookies Save
    document.getElementById("form-sp-cookies")?.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fedauth = (document.getElementById("sp-fedauth")?.value || "").trim();
      const rtfa = (document.getElementById("sp-rtfa")?.value || "").trim();
      const { ok, data } = await SyncApp.api("/sp/save_cookies", { method: "POST", body: { fedauth, rtfa } });
      if (ok && data.success) {
        SyncApp.toast("✅ SharePoint cookies encrypted & saved", "success");
        setTimeout(() => window.location.reload(), 900);
      } else {
        SyncApp.toast(data.error || "Save failed", "error");
      }
    });

    // SharePoint Live Cookie Test
    document.getElementById("btn-test-sp-cookies")?.addEventListener("click", async () => {
      const btn = document.getElementById("btn-test-sp-cookies");
      const out = document.getElementById("sp-cookie-test-result");
      if (btn) btn.disabled = true;
      if (out) { out.style.display = "block"; out.textContent = "⏳ Testing live SharePoint session..."; }
      try {
        const fedauth = (document.getElementById("sp-fedauth")?.value || "").trim();
        const rtfa = (document.getElementById("sp-rtfa")?.value || "").trim();
        const { data } = await SyncApp.api("/sp/test_cookies", { method: "POST", body: { fedauth, rtfa } });
        if (data.success) {
          if (out) out.innerHTML = `<span class="badge badge-ok">✅ ${esc(data.message)}</span>`;
          SyncApp.toast(data.message || "Cookies valid!", "success");
        } else {
          if (out) out.innerHTML = `<span class="badge badge-err">❌ ${esc(data.message || data.error || "Test failed")}</span>`;
          SyncApp.toast(data.message || data.error || "Cookie test failed", "error");
        }
      } finally {
        if (btn) btn.disabled = false;
      }
    });

    // Auto extract cookies (Windows)
    document.getElementById("btn-auto-cookies")?.addEventListener("click", async () => {
      const btn = document.getElementById("btn-auto-cookies");
      if (btn) btn.disabled = true;
      try {
        const { ok, data } = await SyncApp.api("/sp/auto_cookies", { method: "POST" });
        if (ok && data.success) {
          SyncApp.toast(`✅ Cookies extracted from ${data.source}`, "success");
          setTimeout(() => window.location.reload(), 1200);
        } else {
          SyncApp.toast(data.error || "Extraction failed", "error");
        }
      } finally {
        if (btn) btn.disabled = false;
      }
    });

    // Google credentials.json upload
    document.getElementById("form-gd-creds")?.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fileInp = document.getElementById("gd-creds-file");
      const textInp = document.getElementById("gd-creds-text");
      let res;
      if (fileInp && fileInp.files && fileInp.files[0]) {
        const fd = new FormData();
        fd.append("file", fileInp.files[0]);
        res = await SyncApp.api("/gdrive/upload_credentials", { method: "POST", body: fd });
      } else {
        res = await SyncApp.api("/gdrive/upload_credentials", {
          method: "POST",
          body: { content: (textInp?.value || "").trim() },
        });
      }
      if (res.ok && res.data.success) {
        SyncApp.toast("✅ credentials.json saved!", "success");
        setTimeout(() => window.location.reload(), 800);
      } else {
        SyncApp.toast(res.data.error || "Invalid credentials.json", "error");
      }
    });

    // Scheduler
    document.getElementById("scheduler-mode")?.addEventListener("change", toggleSchedulerModeFields);
    document.getElementById("scheduler-form")?.addEventListener("submit", saveScheduler);
    document.getElementById("btn-sched-run-now")?.addEventListener("click", runSchedulerNow);
    document.getElementById("btn-sched-refresh")?.addEventListener("click", refreshSchedulerHealth);

    // Processing settings
    document.getElementById("form-app-settings")?.addEventListener("submit", saveAppSettings);

    // Validate all configs
    document.getElementById("btn-validate-configs")?.addEventListener("click", async () => {
      const box = document.getElementById("config-validation-output");
      const { ok, data } = await SyncApp.api("/validate_configs");
      if (!ok || !data.success) {
        SyncApp.toast(data.error || "Validation error", "error");
        return;
      }
      const v = data.results.validation;
      const totErr = (v.sharepoint.total_errors || 0) + (v.gdrive.total_errors || 0);
      const totWarn = (v.sharepoint.total_warnings || 0) + (v.gdrive.total_warnings || 0);
      if (box) {
        box.style.display = "block";
        const issues = [];
        for (const row of v.sharepoint.configs || []) {
          (row.errors || []).forEach((er) => issues.push(`❌ [SP: ${row.config.name}] ${er}`));
          (row.warnings || []).forEach((wa) => issues.push(`⚠️ [SP: ${row.config.name}] ${wa}`));
        }
        for (const row of v.gdrive.configs || []) {
          (row.errors || []).forEach((er) => issues.push(`❌ [GD: ${row.config.name}] ${er}`));
          (row.warnings || []).forEach((wa) => issues.push(`⚠️ [GD: ${row.config.name}] ${wa}`));
        }
        box.innerHTML = issues.length
          ? `<div style="margin-bottom:6px;">Errors: <strong>${totErr}</strong> • Warnings: <strong>${totWarn}</strong></div>` +
            issues.map((i) => `<div style="font-size:0.82rem;">${esc(i)}</div>`).join("")
          : `<span class="badge badge-ok">✅ All configurations are valid (0 errors, 0 warnings)</span>`;
      }
    });

    // Temp segments cleanup
    document.getElementById("btn-clean-empty-tmp")?.addEventListener("click", async () => {
      const { ok, data } = await SyncApp.api("/api/system/cleanup_temp", {
        method: "POST",
        body: { empty_only: true, min_age_hours: 0 },
      });
      if (ok && data.success) {
        SyncApp.toast(`🧹 Removed ${data.removed_dirs} empty dir(s), freed ${data.freed_mb} MB`, "success");
        refreshSystemHealth();
      } else {
        SyncApp.toast(data.error || "Cleanup failed", "error");
      }
    });

    document.getElementById("btn-clean-old-tmp")?.addEventListener("click", async () => {
      const yes = await SyncApp.confirm("Delete temporary capture segments older than 24 hours? (Completed lectures are untouched.)");
      if (!yes) return;
      const { ok, data } = await SyncApp.api("/api/system/cleanup_temp", {
        method: "POST",
        body: { empty_only: false, min_age_hours: 24 },
      });
      if (ok && data.success) {
        SyncApp.toast(`🧹 Removed ${data.removed_dirs} dir(s), freed ${data.freed_mb} MB`, "success");
        refreshSystemHealth();
        SyncApp.dashboard?.refreshOverviewKpis();
      } else {
        SyncApp.toast(data.error || "Cleanup failed", "error");
      }
    });

    // Backup Export / Import
    document.getElementById("btn-export-backup")?.addEventListener("click", () => {
      window.open("/api/config/export", "_blank");
    });
    document.getElementById("backup-import-file")?.addEventListener("change", async (e) => {
      const file = e.target.files && e.target.files[0];
      if (!file) return;
      try {
        const parsed = JSON.parse(await file.text());
        const { ok, data } = await SyncApp.api("/api/config/import", { method: "POST", body: parsed });
        if (ok && data.success) {
          SyncApp.toast("✅ Configuration backup imported!", "success");
          setTimeout(() => window.location.reload(), 900);
        } else {
          SyncApp.toast(data.error || "Import failed", "error");
        }
      } catch (err) {
        SyncApp.toast("Invalid JSON file: " + err.message, "error");
      } finally {
        e.target.value = "";
      }
    });

    // Desktop notifications
    initNotifyUi();
    document.getElementById("btn-request-notify")?.addEventListener("click", async () => {
      if (!("Notification" in window)) return;
      const perm = await Notification.requestPermission();
      initNotifyUi();
      if (perm === "granted") {
        SyncApp.toast("🔔 Desktop notifications enabled!", "success");
      }
    });

    // Settings section sub-tabs (Connections | Automation & Video | Health & Backup)
    function switchSettingsSection(sec) {
      const target = ["connections", "automation", "health"].includes(sec) ? sec : "connections";
      document.querySelectorAll("[data-settings-section]").forEach((btn) => {
        btn.classList.toggle("active", btn.getAttribute("data-settings-section") === target);
      });
      ["connections", "automation", "health"].forEach((k) => {
        document.getElementById("settings-sec-" + k)?.classList.toggle("active", k === target);
      });
      try {
        window.history.replaceState(null, "", `/settings?section=${target}`);
      } catch (_) {}
      if (target === "automation") refreshSchedulerHealth();
      if (target === "health") refreshSystemHealth();
    }

    document.querySelectorAll("[data-settings-section]").forEach((btn) => {
      btn.addEventListener("click", () => switchSettingsSection(btn.getAttribute("data-settings-section")));
    });

    toggleSchedulerModeFields();
    refreshSchedulerHealth();
    refreshSystemHealth();
  });
})();
