/**
 * Backward-compatible alias: real implementation lives in static/js/i18n.js.
 */
if (window.APP_I18N && !window.UI_LOCALE) {
  window.UI_LOCALE = window.APP_I18N;
}
