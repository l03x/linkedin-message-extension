// Loaded as a classic script in <head> so the panel never flashes the wrong theme.
// "light" | "dark" | "system" (nothing stored). Stored in this extension's localStorage.
(function () {
  var KEY = "theme";

  function get() {
    try {
      var t = localStorage.getItem(KEY);
      return t === "light" || t === "dark" ? t : "system";
    } catch (e) {
      return "system";
    }
  }

  function set(theme) {
    try {
      if (theme === "system") localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, theme);
    } catch (e) {}
    apply(theme);
  }

  function apply(theme) {
    if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
    document.dispatchEvent(new CustomEvent("themechange", { detail: theme }));
  }

  apply(get());
  window.panelTheme = { get: get, set: set };
})();
