// Task-sidebar toggle button — a floating pill pinned to the
// bottom-right corner of the viewport, stacked above the graph-view
// button (public/graph_view.js) and the document-sidebar toggle
// (public/docs_toggle.js). Runs as a vanilla-JS custom_js script.
//
// Visibility: the button is always visible but disabled (greyed,
// tooltip "No tasks yet") when no TaskList aside exists in the DOM.
// Once a TaskList is sent (ingestion starts), the button becomes
// active and the aside is patched: the hardcoded "Tasks" title is
// rewritten to "Task History"/"Aufgabenverlauf", and the status
// badge ("Done"/"Failed"/"Running") becomes a clickable close
// affordance.
//
// Open/close: the TaskList aside is a DOM element in the main chat
// layout (not an ElementSidebar), so we toggle its visibility
// directly via style.display. No server round-trip needed.
//
// Toggle label: "Task History"/"Aufgabenverlauf" when the aside is
// hidden, "Close"/"Schließen" when visible. The open/closed state
// is detected by polling for a visible aside.tasklist /
// aside.tasklist-mobile in the DOM.
(function () {
  "use strict";

  var BUTTON_ID = "tasks-toggle";
  var STATE_POLL_MS = 500;
  var TASKLIST_SELECTOR = "aside.tasklist, aside.tasklist-mobile";
  var TITLE_SELECTOR = "div.font-semibold";

  var hasTasks = false;

  function localized() {
    var htmlLang = (document.documentElement.lang || "de").toLowerCase();
    var isEn = htmlLang.indexOf("en") === 0;
    return {
      open: isEn ? "Task History" : "Aufgabenverlauf",
      close: isEn ? "Close" : "Schließen",
      openTitle: isEn ? "Open task history" : "Aufgabenverlauf öffnen",
      closeTitle: isEn ? "Close task history" : "Aufgabenverlauf schließen",
      noTasks: isEn ? "No tasks yet" : "Noch keine Aufgaben",
      title: isEn ? "Task History" : "Aufgabenverlauf",
      badgeCloseTitle: isEn ? "Close task history" : "Aufgabenverlauf schließen",
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

  function ensureButtonContainer() {
    var existing = document.getElementById(BUTTON_ID);
    if (existing) {
      var cc = themeColors();
      existing.style.borderColor = cc.border;
      existing.style.background = cc.bg;
      return existing;
    }
    var el = document.createElement("div");
    el.id = BUTTON_ID;
    var c = themeColors();
    el.style.cssText = [
      "position:fixed",
      "bottom:6rem",
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
    el.setAttribute("role", "button");
    el.setAttribute("tabindex", "0");
    return el;
  }

  function renderButton() {
    var labels = localized();
    var btn = ensureButtonContainer();
    var icon =
      '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" ' +
      'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
      'stroke-linecap="round" stroke-linejoin="round" style="flex:0 0 auto">' +
      '<path d="M9 11l3 3L22 4"></path>' +
      '<path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"></path>' +
      '</svg>';
    var text = document.createElement("span");
    text.style.cssText = "overflow:hidden;text-overflow:ellipsis";
    text.textContent = labels.open;
    btn.title = labels.openTitle;
    while (btn.firstChild) btn.removeChild(btn.firstChild);
    btn.insertAdjacentHTML("afterbegin", icon);
    btn.appendChild(text);
    btn.style.display = "flex";
    if (!btn.parentElement) document.body.appendChild(btn);
    wireHandlers(btn);
    startStatePoller(btn, text);
    updateDisabledState(btn, text);
  }

  function updateDisabledState(btn, text) {
    var labels = localized();
    if (hasTasks) {
      btn.style.opacity = "1";
      btn.style.cursor = "pointer";
      btn.setAttribute("tabindex", "0");
      var open = isAsideVisible();
      text.textContent = open ? labels.close : labels.open;
      btn.title = open ? labels.closeTitle : labels.openTitle;
    } else {
      btn.style.opacity = "0.5";
      btn.style.cursor = "not-allowed";
      btn.setAttribute("tabindex", "-1");
      text.textContent = labels.open;
      btn.title = labels.noTasks;
    }
  }

  // --- Click + keyboard handlers -------------------------------------------
  var handlersWired = false;

  function wireHandlers(btn) {
    if (handlersWired) return;
    handlersWired = true;
    btn.addEventListener("click", function (e) {
      e.preventDefault();
      e.stopPropagation();
      if (!hasTasks) return;
      toggleSidebar();
    });
    btn.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        e.stopPropagation();
        if (!hasTasks) return;
        toggleSidebar();
      }
    });
  }

  function toggleSidebar() {
    if (isAsideVisible()) {
      closeSidebar();
    } else {
      openSidebar();
    }
  }

  function openSidebar() {
    var asides = document.querySelectorAll(TASKLIST_SELECTOR);
    for (var i = 0; i < asides.length; i++) {
      asides[i].style.display = "";
    }
  }

  function closeSidebar() {
    var asides = document.querySelectorAll(TASKLIST_SELECTOR);
    for (var i = 0; i < asides.length; i++) {
      asides[i].style.display = "none";
    }
  }

  // --- Open/closed state detection + label sync ---------------------------
  function isAsideVisible() {
    var asides = document.querySelectorAll(TASKLIST_SELECTOR);
    for (var i = 0; i < asides.length; i++) {
      var s = asides[i].style.display;
      if (s !== "none" && window.getComputedStyle(asides[i]).display !== "none") {
        return true;
      }
    }
    return false;
  }

  var statePollerStarted = false;
  function startStatePoller(btn, text) {
    if (statePollerStarted) return;
    statePollerStarted = true;
    function sync() {
      updateDisabledState(btn, text);
    }
    setInterval(sync, STATE_POLL_MS);
    sync();
  }

  // --- Header rewrite: "Tasks" -> "Task History" --------------------------
  function rewriteHeader() {
    var labels = localized();
    var asides = document.querySelectorAll(TASKLIST_SELECTOR);
    for (var i = 0; i < asides.length; i++) {
      var titleDivs = asides[i].querySelectorAll(TITLE_SELECTOR);
      for (var j = 0; j < titleDivs.length; j++) {
        var div = titleDivs[j];
        if (div.textContent === "Tasks" || div.textContent === labels.title) {
          div.textContent = labels.title;
        }
      }
    }
  }

  // --- Status badge -> close button ---------------------------------------
  function wireBadgeClose() {
    var labels = localized();
    var asides = document.querySelectorAll(TASKLIST_SELECTOR);
    for (var i = 0; i < asides.length; i++) {
      var headerRow = asides[i].querySelector(
        "div.flex.flex-row.items-center.justify-between"
      );
      if (!headerRow) continue;
      var children = headerRow.children;
      for (var j = 0; j < children.length; j++) {
        var child = children[j];
        if (
          child.tagName !== "DIV" &&
          !child.classList.contains("font-semibold")
        ) {
          if (!child._tasksToggleWired) {
            child._tasksToggleWired = true;
            child.style.cursor = "pointer";
            child.title = labels.badgeCloseTitle;
            child.addEventListener("click", function (e) {
              e.preventDefault();
              e.stopPropagation();
              closeSidebar();
            });
          }
        }
      }
    }
  }

  // --- DOM mutation observer for header/badge rewiring --------------------
  function patchTasklistDOM() {
    rewriteHeader();
    wireBadgeClose();
  }

  var patchDirty = false;
  if (typeof MutationObserver !== "undefined") {
    var patchObserver = new MutationObserver(function () {
      if (patchDirty) return;
      patchDirty = true;
      setTimeout(function () {
        patchDirty = false;
        patchTasklistDOM();
      }, 300);
    });
    var startPatchObserving = function () {
      var target = document.body || document.documentElement;
      if (target) patchObserver.observe(target, { childList: true, subtree: true });
    };
    if (document.body) startPatchObserving();
    else document.addEventListener("DOMContentLoaded", startPatchObserving);
  }

  // --- TaskList presence detection (DOM-based) ----------------------------
  function detectTasks() {
    var asides = document.querySelectorAll(TASKLIST_SELECTOR);
    var found = asides.length > 0;
    if (found !== hasTasks) {
      hasTasks = found;
      if (hasTasks) {
        patchTasklistDOM();
      }
      renderButton();
    }
  }

  // Bootstrap
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      detectTasks();
      renderButton();
    });
  } else {
    detectTasks();
    renderButton();
  }

  // Poll for DOM changes (TaskList may appear/disappear)
  setInterval(detectTasks, 2000);

  // Re-detect on DOM mutations
  var dirty = false;
  if (typeof MutationObserver !== "undefined") {
    var observer = new MutationObserver(function () {
      if (dirty) return;
      dirty = true;
      setTimeout(function () { dirty = false; detectTasks(); }, 300);
    });
    var startObserving = function () {
      var target = document.body || document.documentElement;
      if (target) observer.observe(target, { childList: true, subtree: true });
    };
    if (document.body) startObserving();
    else document.addEventListener("DOMContentLoaded", startObserving);
  }
})();
