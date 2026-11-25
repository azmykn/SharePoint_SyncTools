/**
 * Live Real-Time Activity Log Viewer
 * - Default: Newest First (الأحدث أولاً) with 1s incremental tracking via /api/logs?after_id=
 * - Smooth arrival highlight on every brand-new line
 * - Scroll-aware floating "Jump to latest" pill so reading older lines is never interrupted
 * - One-click toggle between Newest-on-Top and Classic Tail-Bottom, plus Live/Pause control
 */
(function () {
  const SyncApp = window.SyncApp;
  const esc = SyncApp.escapeHtml;
  const ORDER_PREF_KEY = "sync_log_order_v1";
  const MAX_DOM_LINES = 500;

  let sourceFilter = "all";
  let levelFilter = "all";
  let searchQuery = "";
  let lastLogId = 0;
  let seenKeys = new Set();
  let isLive = true;
  let newestFirst = localStorage.getItem(ORDER_PREF_KEY) !== "asc";
  let unseenCount = 0;
  let pollTimer = null;
  let inFlight = false;

  function buildLineEl(entry, highlightNew = false) {
    const div = document.createElement("div");
    div.className = "log-line" + (highlightNew ? " log-line-new" : "");
    if (entry.id) div.dataset.logId = String(entry.id);

    const msg = String(entry.msg || "");
    let colorClass = "";
    if (msg.includes("✅") || msg.includes("🎉")) colorClass = "success";
    else if (msg.includes("❌")) colorClass = "error";
    else if (msg.includes("⚠️") || msg.includes("🛑")) colorClass = "warning";

    let srcClass = "log-source-sys";
    let srcLabel = "[SYS]";
    if (entry.source === "sharepoint") { srcClass = "log-source-sp"; srcLabel = "[SP]"; }
    else if (entry.source === "gdrive") { srcClass = "log-source-gd"; srcLabel = "[GD]"; }

    const rawTime = String(entry.time || "");
    const timePart = rawTime.includes(" ") ? rawTime.split(" ")[1] : rawTime;
    div.innerHTML = `<span class="log-time" title="${esc(rawTime)}">${esc(timePart)}</span><span class="${srcClass}">${srcLabel}</span><span class="log-msg ${colorClass}">${esc(msg)}</span>`;
    return div;
  }

  function isAtLatestEdge(term) {
    if (!term) return true;
    if (newestFirst) {
      return term.scrollTop <= 36;
    }
    return term.scrollHeight - term.scrollTop - term.clientHeight <= 40;
  }

  function scrollToLatest(term, smooth = false) {
    if (!term) return;
    const top = newestFirst ? 0 : term.scrollHeight;
    if (smooth && typeof term.scrollTo === "function") {
      term.scrollTo({ top, behavior: "smooth" });
    } else {
      term.scrollTop = top;
    }
    unseenCount = 0;
    updateJumpPill();
  }

  function updateJumpPill() {
    const btn = document.getElementById("btn-jump-latest");
    const txt = document.getElementById("jump-latest-text");
    if (!btn || !txt) return;
    if (unseenCount > 0) {
      txt.textContent = SyncApp.t("logJumpLatest", { n: unseenCount });
      btn.classList.add("visible");
    } else {
      btn.classList.remove("visible");
    }
  }

  function updateCountBadge(term) {
    const badge = document.getElementById("log-count-badge");
    if (badge && term) badge.textContent = String(term.childElementCount);
  }

  function updateOrderButtonUi() {
    const btn = document.getElementById("btn-log-order-toggle");
    const lbl = document.getElementById("log-order-label");
    if (!btn || !lbl) return;
    btn.firstChild.textContent = newestFirst ? "⬇️ " : "⬆️ ";
    lbl.textContent = SyncApp.t(newestFirst ? "logNewestFirst" : "logTailBottom");
  }

  function updateLiveButtonUi() {
    const btn = document.getElementById("btn-log-live-toggle");
    const lbl = document.getElementById("log-live-label");
    if (!btn || !lbl) return;
    btn.classList.toggle("paused", !isLive);
    lbl.textContent = SyncApp.t(isLive ? "logLiveOn" : "logLivePaused");
  }

  async function fetchLogs({ reset = false } = {}) {
    if (inFlight && !reset) return;
    if (!reset && !isLive) return;

    const term = document.getElementById("terminal");
    if (!term) return;

    inFlight = true;
    const wasReset = reset;
    if (reset) {
      lastLogId = 0;
      seenKeys.clear();
      unseenCount = 0;
      updateJumpPill();
      term.innerHTML = "";
    }

    const params = new URLSearchParams();
    params.set("source", sourceFilter);
    params.set("level", levelFilter);
    params.set("limit", "300");
    if (searchQuery) params.set("q", searchQuery);
    if (!reset && lastLogId > 0) params.set("after_id", String(lastLogId));

    try {
      const { ok, data } = await SyncApp.api("/api/logs?" + params.toString());
      if (!ok || !Array.isArray(data)) return;

      const fresh = [];
      for (const entry of data) {
        const key = entry.id ? `id:${entry.id}` : `${entry.time}|${entry.source}|${entry.msg}`;
        if (seenKeys.has(key)) continue;
        seenKeys.add(key);
        if (entry.id && entry.id > lastLogId) lastLogId = entry.id;
        fresh.push(entry);
      }

      if (!fresh.length) {
        if (wasReset && term.childElementCount === 0) {
          term.innerHTML = `<div class="muted" style="padding:18px;text-align:center;">${esc(SyncApp.t("logEmpty"))}</div>`;
        }
        updateCountBadge(term);
        return;
      }

      // Remove empty placeholder if present
      if (term.firstElementChild && !term.firstElementChild.classList.contains("log-line")) {
        term.innerHTML = "";
      }

      const atLatest = wasReset || isAtLatestEdge(term);
      const prevScrollHeight = term.scrollHeight;
      const highlight = !wasReset;

      if (newestFirst) {
        // Fresh arrives in chronological ASC order; prepend newest at very top
        const frag = document.createDocumentFragment();
        for (let i = fresh.length - 1; i >= 0; i--) {
          frag.appendChild(buildLineEl(fresh[i], highlight));
        }
        term.insertBefore(frag, term.firstChild);

        while (term.childElementCount > MAX_DOM_LINES) {
          term.lastElementChild.remove();
        }

        if (atLatest) {
          term.scrollTop = 0;
        } else {
          // Preserve visual scroll position when user is reading older lines below
          const delta = term.scrollHeight - prevScrollHeight;
          term.scrollTop += delta;
          unseenCount += fresh.length;
          updateJumpPill();
        }
      } else {
        const frag = document.createDocumentFragment();
        for (const entry of fresh) {
          frag.appendChild(buildLineEl(entry, highlight));
        }
        term.appendChild(frag);

        while (term.childElementCount > MAX_DOM_LINES) {
          term.firstElementChild.remove();
        }

        if (atLatest) {
          term.scrollTop = term.scrollHeight;
        } else {
          unseenCount += fresh.length;
          updateJumpPill();
        }
      }

      updateCountBadge(term);
    } catch (_) {
    } finally {
      inFlight = false;
    }
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => {
      if (!document.hidden && isLive) {
        await fetchLogs({ reset: false });
      }
      schedulePoll();
    }, 1000);
  }

  document.addEventListener("DOMContentLoaded", () => {
    const term = document.getElementById("terminal");
    if (!term) return;

    updateOrderButtonUi();
    updateLiveButtonUi();

    document.getElementById("btn-log-live-toggle")?.addEventListener("click", () => {
      isLive = !isLive;
      updateLiveButtonUi();
      if (isLive) fetchLogs({ reset: false });
    });

    document.getElementById("btn-log-order-toggle")?.addEventListener("click", () => {
      newestFirst = !newestFirst;
      localStorage.setItem(ORDER_PREF_KEY, newestFirst ? "desc" : "asc");
      updateOrderButtonUi();
      fetchLogs({ reset: true });
    });

    document.getElementById("btn-jump-latest")?.addEventListener("click", () => {
      scrollToLatest(term, true);
    });

    term.addEventListener("scroll", () => {
      if (unseenCount > 0 && isAtLatestEdge(term)) {
        unseenCount = 0;
        updateJumpPill();
      }
    });

    document.querySelectorAll("[data-log-source]").forEach((btn) => {
      btn.addEventListener("click", () => {
        sourceFilter = btn.getAttribute("data-log-source") || "all";
        document.querySelectorAll("[data-log-source]").forEach((b) =>
          b.classList.toggle("active", b === btn)
        );
        fetchLogs({ reset: true });
      });
    });

    document.getElementById("log-level-filter")?.addEventListener("change", (e) => {
      levelFilter = e.target.value || "all";
      fetchLogs({ reset: true });
    });

    document.getElementById("log-search-input")?.addEventListener(
      "input",
      SyncApp.debounce((e) => {
        searchQuery = (e.target.value || "").trim();
        fetchLogs({ reset: true });
      }, 220)
    );

    document.getElementById("btn-log-copy")?.addEventListener("click", async () => {
      try {
        const res = await fetch("/logs/text?source=" + encodeURIComponent(sourceFilter));
        const txt = await res.text();
        await navigator.clipboard.writeText(txt);
        SyncApp.toast("📋 Logs copied to clipboard", "success");
      } catch (_) {
        SyncApp.toast("Could not copy logs", "error");
      }
    });

    document.getElementById("btn-log-download")?.addEventListener("click", () => {
      window.open("/logs/text?source=" + encodeURIComponent(sourceFilter), "_blank");
    });

    document.getElementById("btn-log-clear")?.addEventListener("click", async () => {
      const yes = await SyncApp.confirm(SyncApp.t("logClearConfirm"));
      if (!yes) return;
      const { ok, data } = await SyncApp.api("/logs/clear", {
        method: "POST",
        body: { source: sourceFilter },
      });
      if (ok && data.success) {
        SyncApp.toast(SyncApp.t("logCleared"), "success");
        fetchLogs({ reset: true });
      } else {
        SyncApp.toast(data.error || "Could not clear logs", "error");
      }
    });

    window.addEventListener("app:langchange", () => {
      updateOrderButtonUi();
      updateLiveButtonUi();
      updateJumpPill();
    });

    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && isLive) fetchLogs({ reset: false });
    });

    fetchLogs({ reset: true });
    schedulePoll();
  });
})();
