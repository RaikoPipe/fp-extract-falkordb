// Document-sidebar toggle button — a floating pill pinned to the
// bottom-right corner of the viewport, modelled on the KG badge
// (public/kg_badge.js). Runs as a vanilla-JS custom_js script injected into
// the page; it never sends a Chainlit message, so it is safe in BOTH the
// starter view and the active chat view (it does NOT dismiss the starter
// screen the way the old OpenDocsButton CustomElement did — sending that as
// a chat message was what swapped the starter view for an empty chat).
//
// Visibility: the button is always visible. On click, if the last poll
// indicates no documents are available, an error toast is shown instead of
// toggling the sidebar. The /api/docs-info endpoint is polled every 15s and
// on DOM mutations to keep the cached state current. Falls back gracefully:
// a 401 hides the button.
//
// Open: window.postMessage({type: "chainlit-toggle-docs-sidebar", open: true})
// — Chainlit's AppWrapper forwards window messages to the backend
// ``window_message`` socket event, which the @cl.on_window_message handler
// in chainlit_app.py filters on the ``type`` string and re-runs
// _refresh_sidebar (whose set_elements call re-opens the ElementSidebar).
//
// Close: programmatically clicks the sidebar's existing close button
// (the ArrowLeft button inside [id="side-view-title"]). This uses Chainlit's
// own setSideView(undefined) path — no server round-trip, no new endpoint.
//
// Toggle label: "Documents"/"Dokumente" when the sidebar is closed,
// "Close"/"Schließen" when open. The open/closed state is detected by
// polling for [id="side-view-title"] in the DOM (ElementSideView renders
// that element when open and nothing when closed — see ElementSideView.tsx).
(function () {
  "use strict";

  var BUTTON_ID = "docs-toggle";
  var POLL_MS = 15000;
  var STATE_POLL_MS = 500;
  var OPEN_MSG_TYPE = "chainlit-toggle-docs-sidebar";
  var SIDEBAR_OPEN_SELECTOR = "[id='side-view-title']";
  var SIDEBAR_CLOSE_BUTTON_SELECTOR =
    SIDEBAR_OPEN_SELECTOR + " button";

  var hasDocuments = false;

  function localized() {
    var htmlLang = (document.documentElement.lang || "de").toLowerCase();
    var isEn = htmlLang.indexOf("en") === 0;
    return {
      open: isEn ? "Documents" : "Dokumente",
      close: isEn ? "Close" : "Schließen",
      openTitle: isEn ? "Open document sidebar" : "Dokumenten-Seitenleiste öffnen",
      closeTitle: isEn ? "Close document sidebar" : "Dokumenten-Seitenleiste schließen",
      noDocs: isEn ? "No documents available" : "Keine Dokumente verfügbar",
    };
  }

  function ensureButtonContainer() {
    var existing = document.getElementById(BUTTON_ID);
    if (existing) return existing;
    var el = document.createElement("div");
    el.id = BUTTON_ID;
    el.style.cssText = [
      "position:fixed",
      "bottom:1rem",
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
      "border:1px solid rgb(242,242,242)",
      "background:rgb(255,255,255)",
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
      '<rect width="18" height="18" x="3" y="3" rx="2"></rect>' +
      '<path d="M15 3v18"></path>' +
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
  }

  function hideButton() {
    var existing = document.getElementById(BUTTON_ID);
    if (existing) existing.style.display = "none";
  }

  // --- Click + keyboard handlers -------------------------------------------
  var handlersWired = false;

  function wireHandlers(btn) {
    if (handlersWired) return;
    handlersWired = true;
    btn.addEventListener("click", function (e) {
      e.preventDefault();
      e.stopPropagation();
      toggleSidebar();
    });
    btn.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        e.stopPropagation();
        toggleSidebar();
      }
    });
  }

  function toggleSidebar() {
    if (!hasDocuments) {
      if (typeof showToast === "function") {
        showToast(localized().noDocs, "error");
      }
      return;
    }
    if (isSidebarOpen()) {
      closeSidebar();
    } else {
      openSidebar();
    }
  }

  function openSidebar() {
    window.postMessage(
      { type: OPEN_MSG_TYPE, open: true },
      window.location.origin
    );
  }

  function closeSidebar() {
    var closeBtn = document.querySelector(SIDEBAR_CLOSE_BUTTON_SELECTOR);
    if (closeBtn) closeBtn.click();
  }

  // --- Open/closed state detection + label sync ---------------------------
  function isSidebarOpen() {
    return !!document.querySelector(SIDEBAR_OPEN_SELECTOR);
  }

  var statePollerStarted = false;
  function startStatePoller(btn, text) {
    if (statePollerStarted) return;
    statePollerStarted = true;
    function sync() {
      var labels = localized();
      var open = isSidebarOpen();
      if (text) text.textContent = open ? labels.close : labels.open;
      btn.title = open ? labels.closeTitle : labels.openTitle;
    }
    setInterval(sync, STATE_POLL_MS);
    sync();
  }

  // --- /api/docs-info polling (visibility) ---------------------------------
  var fetching = false;
  function refresh() {
    if (fetching) return;
    fetching = true;
    fetch("/api/docs-info", { credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) throw new Error("status " + r.status);
        return r.json();
      })
      .then(function (data) {
        hasDocuments = !!(data && data.has_documents);
        renderButton();
      })
      .catch(function (err) {
        if (err && err.message && err.message.indexOf("401") !== -1) {
          hideButton();
          return;
        }
        hasDocuments = false;
        renderButton();
      })
      .finally(function () { fetching = false; });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", refresh);
  } else {
    refresh();
  }

  setInterval(refresh, POLL_MS);

  var dirty = false;
  if (typeof MutationObserver !== "undefined") {
    var observer = new MutationObserver(function () {
      if (dirty) return;
      dirty = true;
      setTimeout(function () { dirty = false; refresh(); }, 300);
    });
    var startObserving = function () {
      var target = document.body || document.documentElement;
      if (target) observer.observe(target, { childList: true, subtree: true });
    };
    if (document.body) startObserving();
    else document.addEventListener("DOMContentLoaded", startObserving);
  }
})();
