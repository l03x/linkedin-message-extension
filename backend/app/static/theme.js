// Theme switch: "light" | "dark" | "system" (no stored value). Stored per browser in localStorage.
(function () {
  var KEY = "theme";

  function stored() {
    try {
      var t = localStorage.getItem(KEY);
      return t === "light" || t === "dark" ? t : "system";
    } catch (e) {
      return "system";
    }
  }

  function apply(theme) {
    if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
    document.querySelectorAll("[data-theme-choice]").forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.dataset.themeChoice === theme));
    });
  }

  // Run immediately (this script is loaded in <head>) so the page never flashes the wrong theme.
  apply(stored());

  document.addEventListener("DOMContentLoaded", function () {
    apply(stored());
    document.querySelectorAll("[data-theme-choice]").forEach(function (b) {
      b.addEventListener("click", function () {
        var theme = b.dataset.themeChoice;
        try {
          if (theme === "system") localStorage.removeItem(KEY);
          else localStorage.setItem(KEY, theme);
        } catch (e) {}
        apply(theme);
        document.dispatchEvent(new CustomEvent("themechange"));
      });
    });
    // Following the OS: charts need to know when it flips.
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
      if (stored() === "system") document.dispatchEvent(new CustomEvent("themechange"));
    });
  });
})();
