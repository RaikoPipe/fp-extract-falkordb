// Chat-flow test button — a floating pill pinned to the bottom-left
// corner of the viewport, modelled on the debug showcase button
// (public/debug_button.js). Runs as a vanilla-JS custom_js script injected
// into the page; it never sends a Chainlit message, so it is safe in BOTH
// the starter view and the active chat view.
//
// Visibility: the button is always visible. On click, if the last poll
// indicates the user is not an admin, an error toast is shown instead of
// filling the composer. The /api/chat-flow-allowed endpoint is polled every
// 15s and on DOM mutations to keep the cached state current. Falls back
// gracefully: a 401 hides the button.
//
// Click: fills the composer with __chat_flow_test__:showcase_flow and
// auto-sends so the mock event stream flows through the real on_message
// handler. The user sees the scenario play out in the chat and can
// visually verify chronological ordering of thinking steps, tool calls,
// chain aggregation, and the final answer.
(function () {
  "use strict";

  var BUTTON_ID = "chat-flow-test";
  var POLL_MS = 15000;
  var TEST_PREFIX = "__chat_flow_test__:showcase_flow";

  var isAllowed = false;

  function localized() {
    var htmlLang = (document.documentElement.lang || "de").toLowerCase();
    var isEn = htmlLang.indexOf("en") === 0;
    return {
      label: isEn ? "Test Chat Flow" : "Chat-Flow testen",
      title: isEn
        ? "Run a mock showcase event stream to verify chat ordering (admin only)"
        : "Mock-Showcase-Event-Stream ausführen, um Chat-Reihenfolge zu prüfen (nur Admin)",
      notAdmin: isEn ? "Admin access required" : "Admin-Zugriff erforderlich",
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
      "bottom:3rem",
      "left:1rem",
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
      '<path d="M4 11a9 9 0 0 1 9 9"></path>' +
      '<path d="M4 4a16 16 0 0 1 16 16"></path>' +
      '<circle cx="5" cy="19" r="1"></circle>' +
      '</svg>';
    var text = document.createElement("span");
    text.style.cssText = "overflow:hidden;text-overflow:ellipsis";
    text.textContent = labels.label;
    btn.title = labels.title;
    while (btn.firstChild) btn.removeChild(btn.firstChild);
    btn.insertAdjacentHTML("afterbegin", icon);
    btn.appendChild(text);
    btn.style.display = "flex";
    if (!btn.parentElement) document.body.appendChild(btn);
    wireHandlers(btn);
  }

  function hideButton() {
    var existing = document.getElementById(BUTTON_ID);
    if (existing) existing.style.display = "none";
  }

  // --- Click handler: fill composer and auto-send ------------------------
  var handlersWired = false;

  function wireHandlers(btn) {
    if (handlersWired) return;
    handlersWired = true;
    btn.addEventListener("click", function (e) {
      e.preventDefault();
      e.stopPropagation();
      fillComposerAndSend();
    });
    btn.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        e.stopPropagation();
        fillComposerAndSend();
      }
    });
  }

  function fillComposerAndSend() {
    if (!isAllowed) {
      if (typeof showToast === "function") {
        showToast(localized().notAdmin, "error");
      }
      return;
    }
    var textarea = findComposerTextarea();
    if (!textarea) return;
    var nativeSetter = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype, "value"
    );
    if (nativeSetter && nativeSetter.set) {
      nativeSetter.set.call(textarea, TEST_PREFIX);
    } else {
      textarea.value = TEST_PREFIX;
    }
    textarea.dispatchEvent(new Event("input", { bubbles: true }));

    // Find the send button and click it after a brief delay so the
    // composer's React state has flushed.
    setTimeout(function () {
      var sendBtn = findSendButton();
      if (sendBtn) {
        sendBtn.click();
      } else {
        // Fallback: dispatch Enter key on the textarea.
        textarea.dispatchEvent(
          new KeyboardEvent("keydown", {
            key: "Enter",
            code: "Enter",
            which: 13,
            keyCode: 13,
            bubbles: true,
          })
        );
      }
      textarea.focus();
    }, 100);
  }

  function findComposerTextarea() {
    var textareas = document.querySelectorAll("textarea");
    for (var i = 0; i < textareas.length; i++) {
      var ta = textareas[i];
      if (ta.offsetParent !== null) return ta;
    }
    return textareas.length > 0 ? textareas[0] : null;
  }

  function findSendButton() {
    // Chainlit's send button is typically a button inside the composer
    // area with an SVG paper-plane / send icon. We look for a clickable
    // button near the textarea that is not disabled.
    var btns = document.querySelectorAll("button");
    for (var i = 0; i < btns.length; i++) {
      var btn = btns[i];
      if (btn.offsetParent === null) continue;
      if (btn.disabled) continue;
      var svg = btn.querySelector("svg");
      if (!svg) continue;
      // Heuristic: the send button is near the bottom of the viewport and
      // is the closest button after the textarea in DOM order.
      var rect = btn.getBoundingClientRect();
      if (rect.top > window.innerHeight * 0.6) {
        return btn;
      }
    }
    return null;
  }

  // --- /api/chat-flow-allowed polling (visibility) -----------------------
  var fetching = false;
  function refresh() {
    if (fetching) return;
    fetching = true;
    fetch("/api/chat-flow-allowed", { credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) throw new Error("status " + r.status);
        return r.json();
      })
      .then(function (data) {
        isAllowed = !!(data && data.allowed);
        renderButton();
      })
      .catch(function (err) {
        if (err && err.message && err.message.indexOf("401") !== -1) {
          hideButton();
          return;
        }
        isAllowed = false;
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