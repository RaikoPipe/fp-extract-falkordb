// Knowledge-graph badge — injected into the top-right of the Chainlit header.
//
// Fetches the authenticated /api/graph-info endpoint (registered in
// auth.register_routes) for the user's last-used knowledge graph name and
// its LLM-maintained description, then renders a small badge in the header.
// Visible in BOTH the starter view and the chat view because it fetches
// from a separate endpoint and mutates the DOM directly — it never sends a
// Chainlit message, so the starter screen is never dismissed.
//
// Refreshes: re-fetches every 15s and on DOM mutations (Chainlit re-renders
// the header on view transitions) so a graph switch (UI dropdown or LLM
// use_graph) is reflected. Falls back gracefully: a 401/503 hides the badge.
//
// Interaction: clicking the badge (or focusing it and pressing Enter/Space)
// opens Chainlit's chat-settings dialog and selects the "graph" tab, so the
// user can quickly switch or create a knowledge graph.
(function () {
  "use strict";

  var BADGE_ID = "kg-badge";
  var POLL_MS = 15000;

  function localized(noneLabel, fetchErrorLabel, graphLabel) {
    // The page locale: Chainlit sets <html lang="...">; default to German
    // (the app's DEFAULT_LANG) when undeterminable.
    var htmlLang = (document.documentElement.lang || "de").toLowerCase();
    var isEn = htmlLang.indexOf("en") === 0;
    return {
      none: isEn ? "No knowledge graph selected" : "Kein Wissensgraph ausgewählt",
      fetchError: isEn ? "Knowledge graph unavailable" : "Wissensgraph nicht verfügbar",
      graph: isEn ? "Knowledge graph" : "Wissensgraph",
      active: isEn ? "Active knowledge graph" : "Aktiver Wissensgraph",
    };
  }

  // Detect Chainlit's active theme so the pill's background/border
  // contrast with the page. Chainlit sets <html data-theme="dark|light">
  // (config default_theme = "dark" — see .chainlit/config.toml); fall back
  // to the OS prefers-color-scheme media query when the attribute is absent
  // (e.g. before the React app hydrates). The MutationObserver below
  // re-runs renderBadge on every <html> mutation, so a theme switch is
  // picked up without dedicated wiring.
  function themeColors() {
    var html = document.documentElement;
    var dark =
      (html.getAttribute("data-theme") || "").toLowerCase() === "dark" ||
      html.classList.contains("dark") ||
      (window.matchMedia &&
        window.matchMedia("(prefers-color-scheme: dark)").matches);
    return dark
      ? { bg: "rgb(30,30,30)", border: "rgb(60,60,60)" }
      : { bg: "rgb(242,242,242)", border: "rgb(242,242,242)" };
  }

  function ensureBadgeContainer() {
    var existing = document.getElementById(BADGE_ID);
    if (existing) {
      // Re-apply theme colors so a theme switch restyles the pill in place.
      var cc = themeColors();
      existing.style.borderColor = cc.border;
      existing.style.background = cc.bg;
      return existing;
    }
    var el = document.createElement("div");
    el.id = BADGE_ID;
    var c = themeColors();
    el.style.cssText = [
      "position:relative",
      "display:flex",
      "align-items:center",
      "gap:6px",
      "padding:4px 10px",
      "border-radius:9999px",
      "font-size:16px",
      "font-weight:500",
      "line-height:1",
      "white-space:nowrap",
      "max-width:340px",
      "overflow:hidden",
      "text-overflow:ellipsis",
      "border:1px solid " + c.border,
      "background:" + c.bg,
      "color:inherit",
      "cursor:pointer",
      "user-select:none",
    ].join(";");
    return el;
  }

  function attachBadge(badge) {
    // Attach to the header, centered. Chainlit's header structure varies by
    // version; try to insert into the header's center area. Fallback: pin
    // to the body top-center with fixed positioning.
    var header = document.querySelector("header") ||
      document.querySelector("[class*='header']") ||
      document.querySelector("nav");
    if (header) {
      // Make the header a flex container so we can center the badge, and
      // insert the badge as a centered child. Using absolute positioning
      // within the (position:relative) header avoids disrupting the
      // existing left/right header clusters.
      if (getComputedStyle(header).position === "static") {
        header.style.position = "relative";
      }
      badge.style.position = "absolute";
      badge.style.top = "50%";
      badge.style.left = "50%";
      badge.style.transform = "translate(-50%, -50%)";
      badge.style.zIndex = "1";
      header.appendChild(badge);
      return;
    }
    // Fallback: fixed top-center pill.
    badge.style.position = "fixed";
    badge.style.top = "10px";
    badge.style.left = "50%";
    badge.style.transform = "translateX(-50%)";
    badge.style.zIndex = "9999";
    document.body.appendChild(badge);
  }

  function renderBadge(data) {
    var labels = localized();
    var badge = ensureBadgeContainer();
    var name = (data && data.last_graph) || "";
    var desc = (data && data.description) || "";
    var dot = document.createElement("span");
    dot.style.cssText = "flex:0 0 auto;width:8px;height:8px;border-radius:50%;" +
      (name ? "background:#22c55e" : "background:#94a3b8");
    var text = document.createElement("span");
    text.style.cssText = "overflow:hidden;text-overflow:ellipsis";
    if (data && data.error) {
      text.textContent = labels.fetchError;
      dot.style.background = "#ef4444";
    } else if (!name) {
      text.textContent = labels.none;
    } else {
      text.textContent = name;
    }
    // Title tooltip shows the active-graph label (with the graph name +
    // description appended when a graph is active) on hover.
    if (name) {
      badge.title = labels.active + ": " + name + (desc ? " — " + desc : "");
    } else {
      badge.title = labels.none;
    }
    // Replace children.
    while (badge.firstChild) badge.removeChild(badge.firstChild);
    badge.appendChild(dot);
    badge.appendChild(text);
    attachBadge(badge);
    // Wire the click-to-open-settings behaviour once.
    ensureClickHandler(badge);
  }

  // --- Click handler: open Chainlit chat settings at the "graph" tab -----
  // Chainlit's settings dialog is a React/Radix component controlled by
  // internal state; there is no public JS API to open it. The reliable,
  // version-stable approach is to programmatically click the settings
  // button Chainlit renders, then click the "graph" tab trigger once the
  // dialog mounts. Two button locations exist depending on config:
  //   - #chat-settings-open-modal  (chat_settings_location == message_composer, the default)
  //   - #chat-settings-header-button (chat_settings_location == sidebar)
  // The dialog renders #chat-settings; the tab triggers are Radix
  // TabsTrigger buttons carrying value="<tab id>" and role="tab". Our
  // "Graph" tab has id="graph" (see _build_settings_widgets).
  var CLICK_GRAPH_TAB_RETRY_MS = 50;
  var CLICK_GRAPH_TAB_MAX_TRIES = 20;
  var clickWired = false;

  function openSettingsAtGraphTab() {
    // 1. Open the settings dialog by clicking Chainlit's settings button.
    var btn =
      document.getElementById("chat-settings-open-modal") ||
      document.getElementById("chat-settings-header-button");
    if (!btn) return;
    btn.click();
    // 2. Wait for the dialog (#chat-settings) to mount, then click the
    //    "graph" tab trigger. The dialog mounts asynchronously (React
    //    state update), so poll until the trigger appears.
    var tries = 0;
    function selectGraphTab() {
      var trigger = document.querySelector(
        '#chat-settings [role="tab"][value="graph"]'
      );
      if (trigger) {
        trigger.click();
        return;
      }
      if (++tries < CLICK_GRAPH_TAB_MAX_TRIES) {
        setTimeout(selectGraphTab, CLICK_GRAPH_TAB_RETRY_MS);
      }
    }
    setTimeout(selectGraphTab, CLICK_GRAPH_TAB_RETRY_MS);
  }

  function ensureClickHandler(badge) {
    if (clickWired) return;
    clickWired = true;
    badge.addEventListener("click", function (e) {
      e.preventDefault();
      e.stopPropagation();
      openSettingsAtGraphTab();
    });
    // Keyboard accessibility: treat the badge as a button.
    badge.setAttribute("role", "button");
    badge.setAttribute("tabindex", "0");
    badge.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        e.stopPropagation();
        openSettingsAtGraphTab();
      }
    });
  }

  var fetching = false;
  function refresh() {
    if (fetching) return;
    fetching = true;
    fetch("/api/graph-info", { credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) throw new Error("status " + r.status);
        return r.json();
      })
      .then(function (data) { renderBadge(data); })
      .catch(function (err) {
        // 401 -> user not logged in yet (login page); hide badge silently.
        // Other errors -> render the fetch-error state.
        if (err && err.message && err.message.indexOf("401") !== -1) {
          var existing = document.getElementById(BADGE_ID);
          if (existing) existing.remove();
          return;
        }
        renderBadge({ error: "unavailable" });
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

  // Poll periodically so a graph switch is reflected even without a DOM
  // mutation (e.g. the LLM use_graph path updates the backend but may not
  // re-render the header).
  setInterval(refresh, POLL_MS);

  // Re-render on DOM mutations (Chainlit re-renders the header on view
  // transitions such as starter -> chat). Throttle with a flag.
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