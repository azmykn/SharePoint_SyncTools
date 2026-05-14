/**
 * UI locale: English (default) with optional Arabic overlay.
 * Set localStorage.ui_lang to "ar" or "en".
 */
(function () {
  const T = {
    en: {
      brand: "Unified Sync",
      navSp: "SharePoint",
      navExplore: "SharePoint explorer",
      navGd: "Google Drive",
      navSettings: "Connection settings",
      navLogs: "Activity log",
      btnSyncAll: "Sync all",
      tabSpTitle: "SharePoint",
      tabGdTitle: "Google Drive",
      tabSettingsTitle: "Connection settings",
      tabLogsTitle: "Activity log",
    },
    ar: {
      brand: "أداة المزامنة الموحدة",
      navSp: "SharePoint",
      navExplore: "مستكشف SharePoint",
      navGd: "Google Drive",
      navSettings: "إعدادات الاتصال",
      navLogs: "سجل العمليات",
      btnSyncAll: "مزامنة الكل",
      tabSpTitle: "SharePoint",
      tabGdTitle: "Google Drive",
      tabSettingsTitle: "إعدادات الاتصال",
      tabLogsTitle: "سجل العمليات",
    },
  };

  function getLang() {
    return localStorage.getItem("ui_lang") === "ar" ? "ar" : "en";
  }

  function setLang(lang) {
    localStorage.setItem("ui_lang", lang === "ar" ? "ar" : "en");
    apply();
  }

  function apply() {
    const lang = getLang();
    const d = T[lang] || T.en;
    document.documentElement.lang = lang;
    document.documentElement.dir = lang === "ar" ? "rtl" : "ltr";
    Object.keys(d).forEach((key) => {
      const el = document.getElementById("i18n-" + key);
      if (el) el.textContent = d[key];
    });
    const title = document.getElementById("current-tab-title");
    if (title && title.dataset.tabKey) {
      const k = title.dataset.tabKey;
      title.textContent = (T[lang] && T[lang][k]) || T.en[k] || title.textContent;
    }
  }

  window.UI_LOCALE = { getLang, setLang, apply, T };

  document.addEventListener("DOMContentLoaded", function () {
    const enBtn = document.getElementById("btn-lang-en");
    const arBtn = document.getElementById("btn-lang-ar");
    if (enBtn) enBtn.addEventListener("click", () => setLang("en"));
    if (arBtn) arBtn.addEventListener("click", () => setLang("ar"));
    apply();
  });
})();
