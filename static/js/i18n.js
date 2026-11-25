/**
 * Unified Bilingual I18N Engine (English + Modern Standard Arabic, RTL/LTR)
 */
(function () {
  const DICT = {
    en: {
      brand: "Unified Sync",
      brandSub: "SharePoint & Google Drive",
      navOverview: "Overview",
      navSp: "SharePoint",
      navExplore: "SharePoint Explorer",
      navGd: "Google Drive",
      navLedger: "Downloaded Lectures",
      navSettings: "Settings",
      navLogs: "Activity Log",
      btnSyncAll: "Sync All",
      btnDryRunAll: "Dry Run",
      btnStopSync: "Stop Sync",
      themeLight: "Light",
      themeDark: "Dark",

      // Overview
      kpiSpStatus: "SharePoint Status",
      kpiGdStatus: "Google Drive Status",
      kpiFolders: "Active Sync Folders",
      kpiLectures: "Downloaded Lectures",
      kpiDisk: "Free Disk Space",
      kpiScheduler: "Auto Scheduler",
      connected: "Connected",
      disconnected: "Not connected",
      enabled: "Enabled",
      disabled: "Disabled",
      liveProgressTitle: "Live Synchronization",
      lastSyncSummary: "Last Sync Summary & History",
      allServices: "All services",
      refresh: "Refresh",
      copyLink: "Copy link",
      exportJson: "Export JSON",
      clear: "Clear",
      searchHistoryPh: "Search history...",
      noReportYet: "No sync report recorded yet.",
      recentRuns: "Recent runs (newest first)",
      noHistoryYet: "No history yet.",
      schedulerSummaryTitle: "Last Scheduled Run Summary",
      noScheduledSummary: "No scheduled sync summary recorded yet.",

      // SharePoint tab
      spSiteMissingTitle: "SharePoint site URL not configured yet",
      spSiteMissingDesc: "Paste a full SharePoint link once in SharePoint Explorer to auto-detect the site root, or save it manually under Settings.",
      spAddEditTitle: "Add / Edit SharePoint Target",
      spFolderName: "Display Name",
      spRelUrl: "Server-Relative URL",
      spLocalPath: "Local Download Path",
      spEntryType: "Target Type",
      spTypeFolder: "Folder (recursive)",
      spTypeFile: "Single File",
      spSharingLink: "Per-Folder Sharing Link (optional :f:/ link)",
      spLocalFlat: "Flat mode — download directly into local path without creating a cloud-named subfolder",
      spVideoOnly: "Videos only — skip non-video files (PDF/PPTX/etc.)",
      saveFolder: "Save Target",
      saveChanges: "Save Changes",
      cancel: "Cancel",
      spConfiguredTitle: "Configured SharePoint Targets",
      spSyncOrderHint: "Drag rows to set sync order (top runs first). Paused targets stay saved and are skipped.",
      syncSpNow: "Sync SharePoint",
      dryRunSp: "Dry Run",
      searchFoldersPh: "Filter targets...",
      openExplorer: "Open Explorer",
      spOrderSaved: "Sync order saved",
      spSyncEnabled: "Sync enabled for this target",
      spSyncPaused: "Sync paused for this target",
      noFoldersYet: "No targets configured yet.",

      // Explorer
      expTitle: "SharePoint Explorer",
      expCard1Title: "1) Paste Sharing Link or Server Path",
      expCard1Help: "Paste a full SharePoint link (e.g. <code>https://…sharepoint.com/:f:/g/personal/…?e=…</code>) and click <strong>Resolve Link</strong>, or paste a <code>/personal/…</code> path directly.",
      expLabelPaste: "Full SharePoint URL or relative path",
      expPhPaste: "https://… or /personal/…",
      expBtnResolve: "Resolve Link",
      expCard2Title: "2) Browse Folder & Select Targets",
      expLabelParent: "Current server-relative path",
      expLabelLocalRoot: "Base local download folder (remembered automatically)",
      expPhLocalRoot: "e.g. /home/user/Downloads/Courses",
      expBtnBrowse: "Browse",
      expBtnBack: "Back",
      expBtnBulk: "Add Selected to Sync",
      expSelectAll: "Select All",
      expClearSel: "Clear Selection",
      expFilterPh: "Filter items in this folder...",
      expBadgeWarn: "Not connected: save FedAuth and rtFa under Settings first.",
      expBadgeOk: "Cookie session ready for browsing",
      expLoading: "Loading folder contents…",
      expEmpty: "This folder is empty.",
      expColType: "Type",
      expColName: "Name",
      expColSize: "Size",
      expColLocal: "Local Path",
      expColAction: "Action",
      expBtnOpen: "Open",
      expTypeFolder: "Folder",
      expTypeFile: "File",

      // Google Drive
      gdBannerNotConn: "Google Drive is not connected",
      gdBannerNotConnSub: "Upload credentials.json if needed, then sign in with Google to enable Drive sync.",
      gdBannerConn: "Google Drive connected",
      gdSignIn: "Sign in with Google",
      gdDisconnect: "Disconnect",
      gdUploadCreds: "Upload credentials.json",
      gdAddEditTitle: "Add / Edit Google Drive Folder",
      gdFolderUrl: "Google Drive Folder URL or ID",
      gdFolderName: "Display Name (auto-fetched)",
      gdConfiguredTitle: "Configured Google Drive Folders",
      syncGdNow: "Sync Google Drive",
      dryRunGd: "Dry Run",
      gdOrderSaved: "Google Drive sync order saved",

      // Ledger
      ledgerTitle: "Downloaded Lectures Ledger",
      ledgerHint: "Recorded items are never re-downloaded automatically even if moved, renamed, trimmed, or deleted. Click Re-download to queue an item for the next sync (existing copy is kept as *_old).",
      ledgerSearchPh: "Search lectures or subjects...",
      ledgerAll: "All",
      ledgerExists: "On Disk",
      ledgerMissing: "Missing",
      ledgerPending: "Pending Re-download",
      ledgerColSeq: "#",
      ledgerColName: "File Name",
      ledgerColDate: "Date",
      ledgerColSize: "Size",
      ledgerColStatus: "Local File",
      ledgerColActions: "Actions",
      ledgerStatusOk: "On Disk",
      ledgerStatusMissing: "Missing",
      ledgerStatusPending: "Queued for Re-download",
      ledgerBtnRedl: "Re-download",
      ledgerBtnCancelRedl: "Cancel Queue",
      ledgerBtnDelete: "Remove Entry",
      ledgerEmpty: "No downloaded lectures recorded yet.",

      // Settings & Health
      setSpTitle: "SharePoint Connection",
      setSiteUrlLabel: "SharePoint Site Root URL",
      setSaveSiteUrl: "Save Site URL",
      setGuestLinkLabel: "Default Guest Sharing Link (:f:/ — optional)",
      setGuestApplyAll: "Also overwrite existing per-folder sharing links (use carefully)",
      setSaveGuestLink: "Save Guest Link",
      setFedAuth: "FedAuth Cookie",
      setRtFa: "rtFa Cookie",
      setSaveCookies: "Save Cookies",
      setTestCookies: "Test Live Connection",
      setAutoCookies: "Extract from Chrome/Edge (Windows)",
      setGdTitle: "Google Drive Connection & Credentials",
      setSchedTitle: "Automated Background Scheduler",
      setSchedEnable: "Enable automatic scheduled sync",
      setSchedMode: "Schedule Mode",
      setSchedModeDaily: "Daily at fixed time",
      setSchedModeInterval: "Every N hours",
      setSchedDailyTime: "Daily Run Time (24h)",
      setSchedInterval: "Interval",
      setSchedTarget: "Target Services",
      setSchedSave: "Save Scheduler",
      setSchedRunNow: "Run Now",
      setProcTitle: "Video Verification & Silence Trimming",
      setTrimEnable: "Trim leading & trailing silence automatically after video download",
      setTrimKeepBackup: "Keep .untrimmed.mp4 backup copy next to trimmed video",
      setTrimNoise: "Silence Threshold (dB)",
      setTrimMinDur: "Min Silence Duration (s)",
      setTrimMargin: "Speech Margin (s)",
      setTrimGap: "Merge Gap (s)",
      setDurPct: "Duration Tolerance (%)",
      setDurSec: "Duration Tolerance (s)",
      setSaveProc: "Save Processing Settings",
      setHealthTitle: "System Health, Maintenance & Backup",
      setCheckConfigs: "Validate All Configs",
      setCleanEmptyTmp: "Clean Empty Temp Dirs",
      setCleanAllTmp: "Purge Old Temp Segments (>24h)",
      setExportBackup: "Export Backup JSON",
      setImportBackup: "Import Backup JSON",
      setNotifyTitle: "Desktop Notifications",
      setEnableNotify: "Enable Desktop Notifications",

      // Logs
      logAll: "All",
      logSp: "SharePoint",
      logGd: "Google Drive",
      logSys: "System",
      logSearchPh: "Filter log lines...",
      logAutoScroll: "Auto-scroll",
      logCopy: "Copy",
      logDownload: "Download .txt",
      logClear: "Clear Log",
      logClearConfirm: "Clear persisted and live log entries for the current filter?",
      logCleared: "Log cleared",

      // Generic
      confirm: "Confirm",
      close: "Close",
      syncSingle: "Sync this target",
      openLocal: "Open local folder",
      pauseSync: "Pause sync",
      resumeSync: "Enable sync",
      edit: "Edit",
      delete: "Delete",
      paused: "Paused",
      active: "Active",
      statusOk: "OK",
      statusOff: "Off",
      readyToSignIn: "Ready to sign in",
      credsMissing: "credentials.json missing",
      manualOnly: "Manual only",
      siteUrlNotSet: "Site URL not set",
      last6: "Last 6",
      last10: "Last 10",
      last20: "Last 20",
      last50: "Last 50",
      subjectsSummary: "{subjects} subjects • {mb} MB",
      diskTotalTmp: "Total {total} GB • tmp {tmp} MB",
      diskLowSpace: "⚠️ Low space (tmp: {tmp} MB)",
      gdAuthTitle: "Google Drive Connection",
      gdAuthDesc: "Connect your Google Drive account to sync and download shared folders automatically.",
      gdAuthSignInReq: "Sign-in required to browse and download folders",
      gdAuthOpenDash: "🚀 Open Dashboard",
      gdAuthReturnDash: "← Return to Dashboard",
      gdAuthManualSummary: "⚙️ Remote server / manual authorization code",
      statusSetup: "Setup",
      spTabTargets: "Sync Targets",
      spTabExplorer: "Explorer & Add from Link",
      addTargetBtn: "Add Target",
      filterAll: "All",
      setTabConnections: "Connections",
      setTabAutomation: "Automation & Video",
      setTabHealth: "Health & Backup",
      configureConnection: "Configure Connection",
      spSiteMissingShort: "Paste a SharePoint link in Explorer or configure it in Settings.",
      gdManualAuthBtn: "Manual Auth Code",
      recentActivityTitle: "Latest Activity",
      openLiveLog: "Open Live Log →",
      logLiveOn: "LIVE",
      logLivePaused: "PAUSED",
      logNewestFirst: "Newest First",
      logTailBottom: "Tail Bottom",
      logJumpLatest: "+{n} new — Jump to latest",
      logEmpty: "No log entries match the current filter.",
      logLevelAll: "Level: All",
      expandAll: "Expand All",
      collapseAll: "Collapse All",
    },
    ar: {
      brand: "أداة المزامنة الموحدة",
      brandSub: "SharePoint و Google Drive",
      navOverview: "نظرة عامة",
      navSp: "SharePoint",
      navExplore: "مستكشف SharePoint",
      navGd: "Google Drive",
      navLedger: "المحاضرات المحمّلة",
      navSettings: "الإعدادات",
      navLogs: "سجل العمليات",
      btnSyncAll: "مزامنة الكل",
      btnDryRunAll: "معاينة فقط",
      btnStopSync: "إيقاف المزامنة",
      themeLight: "فاتح",
      themeDark: "داكن",

      // Overview
      kpiSpStatus: "حالة SharePoint",
      kpiGdStatus: "حالة Google Drive",
      kpiFolders: "المجلدات المفعّلة",
      kpiLectures: "المحاضرات المسجّلة",
      kpiDisk: "المساحة المتاحة",
      kpiScheduler: "الجدولة التلقائية",
      connected: "متصل",
      disconnected: "غير متصل",
      enabled: "مفعّل",
      disabled: "متوقف",
      liveProgressTitle: "المزامنة المباشرة",
      lastSyncSummary: "ملخص وتاريخ آخر مزامنة",
      allServices: "جميع الخدمات",
      refresh: "تحديث",
      copyLink: "نسخ الرابط",
      exportJson: "تصدير JSON",
      clear: "مسح",
      searchHistoryPh: "بحث في السجل...",
      noReportYet: "لا يوجد تقرير مزامنة بعد.",
      recentRuns: "أحدث التشغيلات",
      noHistoryYet: "لا يوجد سجل تشغيل بعد.",
      schedulerSummaryTitle: "ملخص آخر تشغيل مجدول",
      noScheduledSummary: "لا يوجد ملخص للتشغيل المجدول بعد.",

      // SharePoint tab
      spSiteMissingTitle: "رابط موقع SharePoint غير مضبوط بعد",
      spSiteMissingDesc: "الصق رابط SharePoint كاملاً مرة واحدة في مستكشف SharePoint لاكتشاف الجذر تلقائياً، أو أدخله يدوياً من الإعدادات.",
      spAddEditTitle: "إضافة / تعديل هدف SharePoint",
      spFolderName: "اسم العرض",
      spRelUrl: "المسار النسبي على الخادم",
      spLocalPath: "مسار الحفظ المحلي",
      spEntryType: "نوع الهدف",
      spTypeFolder: "مجلد (مع المجلدات الفرعية)",
      spTypeFile: "ملف واحد",
      spSharingLink: "رابط المشاركة الخاص بالمجلد (اختياري :f:/)",
      spLocalFlat: "الوضع المسطّح — التحميل مباشرة داخل المسار دون إنشاء مجلد فرعي باسم المجلد السحابي",
      spVideoOnly: "فيديوهات فقط — تخطّي الملفات غير الفيديوية (PDF/PPTX...)",
      saveFolder: "حفظ الهدف",
      saveChanges: "حفظ التعديلات",
      cancel: "إلغاء",
      spConfiguredTitle: "أهداف SharePoint المهيّأة",
      spSyncOrderHint: "اسحب الصفوف لتحديد ترتيب المزامنة (الأعلى يُنفَّذ أولاً). الأهداف المتوقفة مؤقتاً تبقى محفوظة وتُتخطّى.",
      syncSpNow: "مزامنة SharePoint",
      dryRunSp: "معاينة",
      searchFoldersPh: "تصفية الأهداف...",
      openExplorer: "فتح المستكشف",
      spOrderSaved: "تم حفظ ترتيب المزامنة",
      spSyncEnabled: "تم تفعيل المزامنة لهذا الهدف",
      spSyncPaused: "تم إيقاف المزامنة مؤقتاً لهذا الهدف",
      noFoldersYet: "لا توجد أهداف مهيّأة بعد.",

      // Explorer
      expTitle: "مستكشف SharePoint",
      expCard1Title: "1) لصق رابط المشاركة أو المسار النسبي",
      expCard1Help: "الصق الرابط الكامل من المتصفح (مثل <code>https://…sharepoint.com/:f:/g/personal/…?e=…</code>) ثم اضغط <strong>حل الرابط</strong>، أو الصق مسار <code>/personal/…</code> مباشرة.",
      expLabelPaste: "رابط SharePoint كامل أو مسار نسبي",
      expPhPaste: "https://… أو /personal/…",
      expBtnResolve: "حل الرابط",
      expCard2Title: "2) تصفح المجلد وتحديد الأهداف",
      expLabelParent: "المسار النسبي الحالي",
      expLabelLocalRoot: "المجلد المحلي الأساسي (يُحفظ تلقائياً)",
      expPhLocalRoot: "مثال: /home/user/Downloads/Courses",
      expBtnBrowse: "تصفح",
      expBtnBack: "رجوع",
      expBtnBulk: "إضافة المحدد للمزامنة",
      expSelectAll: "تحديد الكل",
      expClearSel: "إلغاء التحديد",
      expFilterPh: "تصفية عناصر هذا المجلد...",
      expBadgeWarn: "غير متصل: احفظ FedAuth و rtFa من تبويب الإعدادات أولاً.",
      expBadgeOk: "جلسة الكوكيز جاهزة للتصفح",
      expLoading: "جارٍ تحميل محتويات المجلد…",
      expEmpty: "هذا المجلد فارغ.",
      expColType: "النوع",
      expColName: "الاسم",
      expColSize: "الحجم",
      expColLocal: "المسار المحلي",
      expColAction: "إجراء",
      expBtnOpen: "فتح",
      expTypeFolder: "مجلد",
      expTypeFile: "ملف",

      // Google Drive
      gdBannerNotConn: "Google Drive غير متصل",
      gdBannerNotConnSub: "ارفع ملف credentials.json عند الحاجة، ثم سجّل الدخول بحساب Google لتفعيل المزامنة.",
      gdBannerConn: "Google Drive متصل",
      gdSignIn: "تسجيل الدخول بـ Google",
      gdDisconnect: "قطع الاتصال",
      gdUploadCreds: "رفع credentials.json",
      gdAddEditTitle: "إضافة / تعديل مجلد Google Drive",
      gdFolderUrl: "رابط مجلد Google Drive أو المعرّف",
      gdFolderName: "اسم العرض (يُجلب تلقائياً)",
      gdConfiguredTitle: "مجلدات Google Drive المهيّأة",
      syncGdNow: "مزامنة Google Drive",
      dryRunGd: "معاينة",
      gdOrderSaved: "تم حفظ ترتيب مجلدات Google Drive",

      // Ledger
      ledgerTitle: "سجل المحاضرات المحمّلة",
      ledgerHint: "لا يُعاد تحميل أي محاضرة مسجّلة هنا تلقائياً حتى لو نُقلت أو أُعيد تسميتها أو حُذفت. استخدم «إعادة التحميل» لإدراجها في المزامنة القادمة (مع حفظ النسخة السابقة بلاحقة _old).",
      ledgerSearchPh: "بحث في المحاضرات أو المواد...",
      ledgerAll: "الكل",
      ledgerExists: "موجود على القرص",
      ledgerMissing: "غير موجود",
      ledgerPending: "بانتظار إعادة التحميل",
      ledgerColSeq: "#",
      ledgerColName: "اسم الملف",
      ledgerColDate: "التاريخ",
      ledgerColSize: "الحجم",
      ledgerColStatus: "حالة الملف",
      ledgerColActions: "الإجراءات",
      ledgerStatusOk: "موجود",
      ledgerStatusMissing: "مفقود",
      ledgerStatusPending: "بانتظار التحميل",
      ledgerBtnRedl: "إعادة التحميل",
      ledgerBtnCancelRedl: "إلغاء الطلب",
      ledgerBtnDelete: "حذف السجل",
      ledgerEmpty: "لا توجد محاضرات مسجّلة بعد.",

      // Settings & Health
      setSpTitle: "اتصال SharePoint",
      setSiteUrlLabel: "رابط جذر موقع SharePoint",
      setSaveSiteUrl: "حفظ رابط الموقع",
      setGuestLinkLabel: "رابط المشاركة العام الافتراضي (:f:/ — اختياري)",
      setGuestApplyAll: "تحديث روابط المشاركة الخاصة بكل المجلدات أيضاً (بحذر)",
      setSaveGuestLink: "حفظ رابط المشاركة",
      setFedAuth: "كوكي FedAuth",
      setRtFa: "كوكي rtFa",
      setSaveCookies: "حفظ الكوكيز",
      setTestCookies: "اختبار الاتصال الحي",
      setAutoCookies: "استخراج تلقائي من Chrome/Edge (Windows)",
      setGdTitle: "اتصال وبيانات اعتماد Google Drive",
      setSchedTitle: "الجدولة التلقائية في الخلفية",
      setSchedEnable: "تفعيل المزامنة التلقائية المجدولة",
      setSchedMode: "نمط الجدولة",
      setSchedModeDaily: "يومياً في وقت محدد",
      setSchedModeInterval: "كل عدد معين من الساعات",
      setSchedDailyTime: "وقت التشغيل اليومي (24 ساعة)",
      setSchedInterval: "الفاصل الزمني",
      setSchedTarget: "الخدمات المستهدفة",
      setSchedSave: "حفظ إعدادات الجدولة",
      setSchedRunNow: "تشغيل الآن",
      setProcTitle: "التحقق من المدة وقص الصمت",
      setTrimEnable: "قص الصمت في بداية ونهاية الفيديو تلقائياً بعد التحميل",
      setTrimKeepBackup: "الاحتفاظ بنسخة احتياطية .untrimmed.mp4 بجانب الفيديو المقصوص",
      setTrimNoise: "عتبة الصمت (dB)",
      setTrimMinDur: "أقل مدة صمت (ثانية)",
      setTrimMargin: "هامش الكلام (ثانية)",
      setTrimGap: "فجوة الدمج (ثانية)",
      setDurPct: "نسبة سماحية المدة (%)",
      setDurSec: "سماحية المدة بالثواني",
      setSaveProc: "حفظ إعدادات المعالجة",
      setHealthTitle: "صحة النظام والصيانة والنسخ الاحتياطي",
      setCheckConfigs: "فحص جميع الإعدادات",
      setCleanEmptyTmp: "تنظيف المجلدات المؤقتة الفارغة",
      setCleanAllTmp: "حذف المؤقتات القديمة (>24 ساعة)",
      setExportBackup: "تصدير نسخة احتياطية JSON",
      setImportBackup: "استيراد نسخة احتياطية JSON",
      setNotifyTitle: "إشعارات سطح المكتب",
      setEnableNotify: "تفعيل إشعارات سطح المكتب",

      // Logs
      logAll: "الكل",
      logSp: "SharePoint",
      logGd: "Google Drive",
      logSys: "النظام",
      logSearchPh: "تصفية سطور السجل...",
      logAutoScroll: "تمرير تلقائي",
      logCopy: "نسخ",
      logDownload: "تنزيل .txt",
      logClear: "مسح السجل",
      logClearConfirm: "هل تريد مسح سجل العمليات للفلتر الحالي؟",
      logCleared: "تم مسح السجل",

      // Generic
      confirm: "تأكيد",
      close: "إغلاق",
      syncSingle: "مزامنة هذا الهدف فقط",
      openLocal: "فتح المجلد المحلي",
      pauseSync: "إيقاف مؤقت",
      resumeSync: "تفعيل المزامنة",
      edit: "تعديل",
      delete: "حذف",
      paused: "متوقف",
      active: "نشط",
      statusOk: "متصل",
      statusOff: "غير متصل",
      readyToSignIn: "جاهز لتسجيل الدخول",
      credsMissing: "ملف credentials.json مفقود",
      manualOnly: "تشغيل يدوي فقط",
      siteUrlNotSet: "لم يتم ضبط رابط الموقع",
      last6: "آخر 6",
      last10: "آخر 10",
      last20: "آخر 20",
      last50: "آخر 50",
      subjectsSummary: "{subjects} مادة • {mb} MB",
      diskTotalTmp: "الإجمالي {total} GB • مؤقت {tmp} MB",
      diskLowSpace: "⚠️ مساحة منخفضة (مؤقت: {tmp} MB)",
      gdAuthTitle: "ربط حساب Google Drive",
      gdAuthDesc: "اربط حساب Google Drive لمزامنة وتحميل المجلدات المشتركة تلقائياً.",
      gdAuthSignInReq: "يلزم تسجيل الدخول لتصفح وتحميل المجلدات",
      gdAuthOpenDash: "🚀 فتح لوحة التحكم",
      gdAuthReturnDash: "← العودة للوحة التحكم",
      gdAuthManualSummary: "⚙️ خادم بعيد / إدخال رمز المصادقة يدوياً",
      statusSetup: "إعداد",
      spTabTargets: "المجلدات المجدولة",
      spTabExplorer: "المستكشف والإضافة برابط",
      addTargetBtn: "إضافة هدف",
      filterAll: "الكل",
      setTabConnections: "الاتصالات والحسابات",
      setTabAutomation: "الجدولة ومعالجة الفيديو",
      setTabHealth: "الصحة والنسخ الاحتياطي",
      configureConnection: "إعداد الاتصال",
      spSiteMissingShort: "الصق رابط SharePoint في المستكشف أو أضفه من الإعدادات.",
      gdManualAuthBtn: "إدخال الرمز يدوياً",
      recentActivityTitle: "أحدث النشاطات",
      openLiveLog: "فتح السجل المباشر ←",
      logLiveOn: "مباشر",
      logLivePaused: "متوقف مؤقتاً",
      logNewestFirst: "الأحدث بالأعلى",
      logTailBottom: "تتبع الأسفل",
      logJumpLatest: "+{n} جديد — انتقال للأحدث",
      logEmpty: "لا توجد أسطر سجل مطابقة للتصفية الحالية.",
      logLevelAll: "المستوى: الكل",
      expandAll: "فتح الكل",
      collapseAll: "طي الكل",
    },
  };

  function getLang() {
    return localStorage.getItem("ui_lang") === "ar" ? "ar" : "en";
  }

  function t(key, vars) {
    const lang = getLang();
    let val = (DICT[lang] && DICT[lang][key]) || DICT.en[key] || key;
    if (vars) {
      Object.keys(vars).forEach((k) => {
        val = val.replace(new RegExp("\\{" + k + "\\}", "g"), String(vars[k]));
      });
    }
    return val;
  }

  function apply() {
    const lang = getLang();
    document.documentElement.lang = lang;
    document.documentElement.dir = lang === "ar" ? "rtl" : "ltr";

    document.querySelectorAll("[data-i18n]").forEach((el) => {
      const k = el.getAttribute("data-i18n");
      if (k) el.textContent = t(k);
    });
    document.querySelectorAll("[data-i18n-html]").forEach((el) => {
      const k = el.getAttribute("data-i18n-html");
      if (k) el.innerHTML = t(k);
    });
    document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
      const k = el.getAttribute("data-i18n-placeholder");
      if (k) el.placeholder = t(k);
    });
    document.querySelectorAll("[data-i18n-title]").forEach((el) => {
      const k = el.getAttribute("data-i18n-title");
      if (k) el.title = t(k);
    });

    const btnEn = document.getElementById("btn-lang-en");
    const btnAr = document.getElementById("btn-lang-ar");
    if (btnEn) btnEn.classList.toggle("active", lang === "en");
    if (btnAr) btnAr.classList.toggle("active", lang === "ar");

    const btnToggle = document.getElementById("btn-lang-toggle");
    if (btnToggle) {
      btnToggle.textContent = lang === "ar" ? "English" : "العربية";
    }

    const titleEl = document.getElementById("current-tab-title");
    if (titleEl && titleEl.dataset.tabKey) {
      titleEl.textContent = t(titleEl.dataset.tabKey);
    }

    window.dispatchEvent(new CustomEvent("app:langchange", { detail: { lang } }));
  }

  function setLang(lang) {
    localStorage.setItem("ui_lang", lang === "ar" ? "ar" : "en");
    apply();
  }

  window.APP_I18N = { getLang, setLang, apply, t, T: DICT };
  window.UI_LOCALE = window.APP_I18N;

  document.addEventListener("DOMContentLoaded", () => {
    const btnEn = document.getElementById("btn-lang-en");
    const btnAr = document.getElementById("btn-lang-ar");
    if (btnEn) btnEn.addEventListener("click", () => setLang("en"));
    if (btnAr) btnAr.addEventListener("click", () => setLang("ar"));
    const btnToggle = document.getElementById("btn-lang-toggle");
    if (btnToggle) {
      btnToggle.addEventListener("click", () => setLang(getLang() === "ar" ? "en" : "ar"));
    }
    apply();
  });
})();
