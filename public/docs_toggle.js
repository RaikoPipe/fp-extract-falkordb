// Document-sidebar toggle button — a floating pill pinned to the
// bottom-right corner of the viewport, modelled on the KG badge
// (public/kg_badge.js). Runs as a vanilla-JS custom_js script injected into
// the page; it never sends a Chainlit message, so it is safe in BOTH the
// starter view and the active chat view (it does NOT dismiss the starter
// screen the way the old OpenDocsButton CustomElement did — sending that as
// a chat message was what swapped the starter view for an empty chat).
//
// Visibility: fetches the authenticated /api/docs-info endpoint
// (registered in auth.register_routes) for whether the user's last-used
// knowledge graph has any ingested documents. The button is hidden when
// there is nothing to show (ingested-rows proxy — the endpoint runs outside
// any Chainlit session, so it cannot see the current thread's uploads; the
// sidebar already auto-opens on upload via _refresh_sidebar, so the toggle
// is not needed in that narrow case). Re-fetches every 15s and on DOM
// mutations (Chainlit re-renders on view transitions). Falls back
// gracefully: a 401/503 hides the button.
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

  function localized() {
    // The page locale: Chainlit sets <html lang="...">; default to German
    // (the app's DEFAULT_LANG) when undeterminable.
    var htmlLang = (document.documentElement.lang || "de").toLowerCase();
    var isEn = htmlLang.indexOf("en") === 0;
    return {
      open: isEn ? "Documents" : "Dokumente",
      close: isEn ? "Close" : "Schließen",
      openTitle: isEn ? "Open document sidebar" : "Dokumenten-Seitenleiste öffnen",
      closeTitle: isEn ? "Close document sidebar" : "Dokumenten-Seitenleiste schließen",
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
    // Treat the pill as a button for accessibility.
    el.setAttribute("role", "button");
    el.setAttribute("tabindex", "0");
    return el;
  }

  function renderButton(hasDocs) {
    var labels = localized();
    var btn = ensureButtonContainer();
    // Icon (a small panel/document glyph rendered as inline SVG so we don't
    // depend on lucide or any CSS). Mirrors the PanelRight icon the old
    // OpenDocsButton used.
    var icon =
      '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" ' +
      'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
      'stroke-linecap="round" stroke-linejoin="round" style="flex:0 0 auto">' +
      '<rect width="18" height="18" x="3" y="3" rx="2"></rect>' +
      '<path d="M15 3v18"></path>' +
      '</svg>';
    var text = document.createElement("span");
    text.style.cssText = "overflow:hidden;text-overflow:ellipsis";
    text.textContent = labels.open; // default = closed-state label
    btn.title = labels.openTitle;
    // Replace children (preserve the container + handlers).
    while (btn.firstChild) btn.removeChild(btn.firstChild);
    btn.insertAdjacentHTML("afterbegin", icon);
    btn.appendChild(text);
    // Hide when there is nothing to show.
    btn.style.display = hasDocs ? "flex" : "none";
    // Attach to body (fixed positioning, so any parent is fine).
    if (!btn.parentElement) document.body.appendChild(btn);
    // Wire handlers once.
    wireHandlers(btn);
    // Start the open/closed state poller once.
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
    if (isSidebarOpen()) {
      closeSidebar();
    } else {
      openSidebar();
    }
  }

  function openSidebar() {
    // Ask the server to re-run _refresh_sidebar (which re-opens the
    // ElementSidebar via set_elements). window.postMessage is forwarded by
    // Chainlit's AppWrapper to the backend window_message socket event.
    window.postMessage(
      { type: OPEN_MSG_TYPE, open: true },
      window.location.origin
    );
  }

  function closeSidebar() {
    // Click the sidebar's own close button (the ArrowLeft button inside
    // #side-view-title). This triggers Chainlit's setSideView(undefined)
    // and tears down the sidebar via its own React state — no server
    // round-trip, no new endpoint.
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
    // Poll the DOM for sidebar state. ElementSideView mounts/unmounts
    // [id="side-view-title"] on open/close; a MutationObserver would also
    // work but a light poll is simpler and matches the KG badge's cadence.
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
        renderButton(!!(data && data.has_documents));
      })
      .catch(function (err) {
        // 401 -> user not logged in yet (login page); hide button silently.
        // Other errors -> hide (no docs signal).
        if (err && err.message && err.message.indexOf("401") !== -1) {
          hideButton();
          return;
        }
        hideButton();
      })
      .finally(function () { fetching = false; });
  }

  // Initial render once the DOM is ready (defer guarantees DOMContentLoaded
  // has not yet fired, so listen for it).
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", refresh);
  } else {
    refresh();
  }

  // Poll periodically so a graph switch / ingestion is reflected even
  // without a DOM mutation.
  setInterval(refresh, POLL_MS);

  // Re-render on DOM mutations (Chainlit re-renders on view transitions
  // such as starter -> chat). Throttle with a flag.
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