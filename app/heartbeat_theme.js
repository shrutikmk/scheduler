(function (global) {
  "use strict";

  var LS_KEY = "scheduler-theme-mode";
  var MORNING_START = 7;
  var AFTERNOON_START = 12;
  var NIGHT_START = 19;
  var THEMES = ["light", "afternoon", "dark"];
  var MODES = ["auto"].concat(THEMES);

  function autoThemeNow(date) {
    var h = (date || new Date()).getHours();
    if (h >= MORNING_START && h < AFTERNOON_START) return "light";
    if (h >= AFTERNOON_START && h < NIGHT_START) return "afternoon";
    return "dark";
  }

  function normalizeTheme(theme) {
    return THEMES.indexOf(theme) >= 0 ? theme : "light";
  }

  function readMode() {
    try {
      var raw = localStorage.getItem(LS_KEY);
      if (MODES.indexOf(raw) >= 0) return raw;
    } catch (_) {}
    return "auto";
  }

  function resolveTheme(mode) {
    return mode === "auto" ? autoThemeNow() : normalizeTheme(mode);
  }

  function paintDocumentTheme() {
    try {
      document.documentElement.dataset.theme = resolveTheme(readMode());
    } catch (_) {
      document.documentElement.dataset.theme = "light";
    }
  }

  global.HeartbeatTheme = {
    LS_KEY: LS_KEY,
    MORNING_START: MORNING_START,
    AFTERNOON_START: AFTERNOON_START,
    NIGHT_START: NIGHT_START,
    THEMES: THEMES,
    MODES: MODES,
    autoThemeNow: autoThemeNow,
    normalizeTheme: normalizeTheme,
    readMode: readMode,
    resolveTheme: resolveTheme,
    paintDocumentTheme: paintDocumentTheme,
  };
})(typeof window !== "undefined" ? window : globalThis);
