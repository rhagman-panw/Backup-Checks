(function () {
  "use strict";
  var form = document.getElementById("search-form");
  if (!form) return;

  function syncMode() {
    var checked = form.querySelector("input[name=mode]:checked");
    var mode = checked ? checked.value : "panorama";
    form.querySelectorAll(".mode-panel").forEach(function (panel) {
      var active = panel.getAttribute("data-mode") === mode;
      panel.hidden = !active;
      panel.querySelectorAll("input").forEach(function (el) { el.disabled = !active; });
    });
  }

  form.querySelectorAll("input[name=mode]").forEach(function (r) {
    r.addEventListener("change", syncMode);
  });
  syncMode();

  form.addEventListener("submit", function () {
    var btn = document.getElementById("submit-btn");
    btn.disabled = true;
    document.getElementById("busy").hidden = false;
    // Clear the password field after submit so it is not kept in the page.
    setTimeout(function () {
      var pw = form.querySelector("input[name=password]");
      if (pw) pw.value = "";
    }, 0);
  });
})();
