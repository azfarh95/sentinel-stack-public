// Sentinel Finance — global privacy toggle
// Applies a blur filter to all currency values across every Mini App page.
// State persists in localStorage["sentinel-private"] = "1" | "0".

(function () {
  var KEY = "sentinel-private";

  function apply() {
    if (localStorage.getItem(KEY) === "1") {
      document.body.classList.add("private");
    } else {
      document.body.classList.remove("private");
    }
  }

  window.togglePrivacy = function () {
    var on = document.body.classList.toggle("private");
    localStorage.setItem(KEY, on ? "1" : "0");
  };

  function injectFab() {
    // Skip if the page already has its own button
    if (document.querySelector(".privacy-btn,[data-privacy-toggle]")) return;
    var btn = document.createElement("button");
    btn.className = "privacy-fab";
    btn.setAttribute("data-privacy-toggle", "1");
    btn.setAttribute("title", "Hide / show balances");
    btn.setAttribute("aria-label", "Toggle privacy");
    btn.textContent = "👁";
    btn.onclick = window.togglePrivacy;
    document.body.appendChild(btn);
  }

  document.addEventListener("DOMContentLoaded", function () {
    apply();
    injectFab();
    try { Telegram.WebApp.ready(); Telegram.WebApp.expand(); } catch (e) {}
  });
})();
