// Knowledge-graph viewer — a floating toggle button that opens the
// FalkorDB web UI in a new tab. Replaces the former Cytoscape-based
// side panel; the toggle button is retained in the same position.
//
// Auth behaviour: polls /api/graph-info on init. A 401 hides the
// toggle; success shows it. The button is always visible once auth
// succeeds (no panel to open — just a link).
(function () {
  "use strict";

  var TOGGLE_BTN_ID = "kg-view-toggle";
  var POLL_MS = 15000;

  var toggleBtnEl = null;
  var fetching = false;

  function localized() {
    var htmlLang = (document.documentElement.lang || "de").toLowerCase();
    var isEn = htmlLang.indexOf("en") === 0;
    return {
      open: isEn ? "Graph" : "Graph",
      openTitle: isEn ? "Open FalkorDB graph viewer" : "FalkorDB Graph-Anzeige öffnen",
    };
  }

  // Detect Chainlit's active theme so the pill's background/border
  // contrast with the page. See kg_badge.js#themeColors for rationale.
  function themeColors() {
    var html = document.documentElement;
    var dark =
      (html.getAttribute("data-theme") || "").toLowerCase() === "dark" ||
      html.classList.contains("dark") ||
      (window.matchMedia &&
        window.matchMedia("(prefers-color-scheme: dark)").matches);
    return dark
      ? { bg: "rgb(30,30,30)", border: "rgb(60,60,60)" }
      : { bg: "rgb(255,255,255)", border: "rgb(242,242,242)" };
  }

  function ensureToggle() {
    var existing = document.getElementById(TOGGLE_BTN_ID);
    if (existing) {
      var cc = themeColors();
      existing.style.borderColor = cc.border;
      existing.style.background = cc.bg;
      return existing;
    }
    var btn = document.createElement("div");
    btn.id = TOGGLE_BTN_ID;
    var c = themeColors();
    btn.style.cssText = [
      "position:fixed",
      "bottom:3.5rem",
      "right:1rem",
      "z-index:9999",
      "display:flex",
      "align-items:center",
      "gap:6px",
      "padding:6px 12px",
      "border-radius:9999px",
      "font-size:14px",
      "font-weight:500",
      "line-height:1",
      "white-space:nowrap",
      "border:1px solid " + c.border,
      "background:" + c.bg,
      "color:inherit",
      "cursor:pointer",
      "user-select:none",
      "box-shadow:0 1px 3px rgba(0,0,0,0.12)",
    ].join(";");
    btn.setAttribute("role", "button");
    btn.setAttribute("tabindex", "0");
    var icon =
      '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" ' +
      'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
      'stroke-linecap="round" stroke-linejoin="round" style="flex:0 0 auto">' +
      '<circle cx="6" cy="6" r="2.5"></circle>' +
      '<circle cx="18" cy="6" r="2.5"></circle>' +
      '<circle cx="12" cy="18" r="2.5"></circle>' +
      '<line x1="7.5" y1="7" x2="10.5" y2="16"></line>' +
      '<line x1="16.5" y1="7" x2="13.5" y2="16"></line>' +
      '<line x1="8" y1="6" x2="16" y2="6"></line>' +
      '</svg>';
    btn.insertAdjacentHTML("afterbegin", icon);
    var label = document.createElement("span");
    label.style.cssText = "overflow:hidden;text-overflow:ellipsis";
    label.textContent = localized().open;
    btn.appendChild(label);
    btn.title = localized().openTitle;
    btn.addEventListener("click", function (e) {
      e.preventDefault();
      e.stopPropagation();
      var url = window.location.protocol + "//" + window.location.hostname + ":3000";
      window.open(url, "_blank", "noopener,noreferrer");
    });
    btn.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        e.stopPropagation();
        var url = window.location.protocol + "//" + window.location.hostname + ":3000";
        window.open(url, "_blank", "noopener,noreferrer");
      }
    });
    document.body.appendChild(btn);
    return btn;
  }

  function refresh() {
    if (fetching) return;
    fetching = true;
    fetch("/api/graph-info", { credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) throw new Error("status " + r.status);
        toggleBtnEl = ensureToggle();
        toggleBtnEl.style.display = "flex";
      })
      .catch(function (err) {
        if (err && err.message && err.message.indexOf("401") !== -1) {
          if (toggleBtnEl) toggleBtnEl.style.display = "none";
          return;
        }
        // Other errors: keep the button visible (FalkorDB may be down
        // but the link is still useful once it comes back).
        toggleBtnEl = ensureToggle();
        toggleBtnEl.style.display = "flex";
      })
      .finally(function () { fetching = false; });
  }

  // Bootstrap
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", refresh);
  } else {
    refresh();
  }

  setInterval(refresh, POLL_MS);

  // Re-show on DOM mutations (Chainlit re-renders the header on view
  // transitions). Throttle with a flag.
  var dirty = false;
  if (typeof MutationObserver !== "undefined") {
    var observer = new MutationObserver(function () {
      if (dirty) return;
      dirty = true;
      setTimeout(function () {
        dirty = false;
        if (toggleBtnEl) toggleBtnEl.style.display = "flex";
      }, 300);
    });
    var startObserving = function () {
      var target = document.body || document.documentElement;
      if (target) observer.observe(target, { childList: true, subtree: true });
    };
    if (document.body) startObserving();
    else document.addEventListener("DOMContentLoaded", startObserving);
  }
})();
