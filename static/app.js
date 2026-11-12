(function () {
  "use strict";
  var form = document.getElementById("search-form");
  if (!form) return;

  function setActive(panel, active) {
    panel.hidden = !active;
    panel.querySelectorAll("input, select, textarea").forEach(function (el) { el.disabled = !active; });
  }

  function value(name, fallback) {
    var checked = form.querySelector("input[name=" + name + "]:checked");
    return checked ? checked.value : fallback;
  }

  function sync() {
    var mode = value("mode", "panorama");
    form.querySelectorAll(".mode-panel").forEach(function (panel) {
      setActive(panel, panel.getAttribute("data-mode") === mode);
    });
    if (mode === "panorama") {
      var method = value("method", "proxy");
      form.querySelectorAll(".method-panel").forEach(function (panel) {
        setActive(panel, panel.getAttribute("data-method") === method);
      });
    }
  }

  form.querySelectorAll("input[name=mode], input[name=method]").forEach(function (r) {
    r.addEventListener("change", sync);
  });
  sync();

  form.addEventListener("submit", function () {
    document.getElementById("submit-btn").disabled = true;
    document.getElementById("busy").hidden = false;
    // Clear secret fields after submit so they are not kept in the page.
    setTimeout(function () {
      form.querySelectorAll("input[type=password], input[name=ssh_key]").forEach(function (el) {
        el.value = "";
      });
    }, 0);
  });
})();
