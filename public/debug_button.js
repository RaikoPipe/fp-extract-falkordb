// Debug "Run Showcase" button — a floating pill pinned to the bottom-left
// corner of the viewport, modelled on the document-sidebar toggle
// (public/docs_toggle.js). Runs as a vanilla-JS custom_js script injected
// into the page; it never sends a Chainlit message, so it is safe in BOTH
// the starter view and the active chat view.
//
// Visibility: the button is always visible. On click, if the last poll
// indicates the user is not an admin, an error toast is shown instead of
// filling the composer. The /api/debug-allowed endpoint is polled every 15s
// and on DOM mutations to keep the cached state current. Falls back
// gracefully: a 401 hides the button.
//
// Click: fetches /api/debug-allowed to get the showcase prompt, then
// injects it into the Chainlit chat composer <textarea> WITHOUT sending.
// The user can review, attach files, and press Enter to start the run.
(function () {
  "use strict";

  var BUTTON_ID = "debug-showcase";
  var POLL_MS = 15000;

  var isAllowed = false;

  function localized() {
    var htmlLang = (document.documentElement.lang || "de").toLowerCase();
    var isEn = htmlLang.indexOf("en") === 0;
    return {
      label: isEn ? "Run Showcase" : "Showcase ausführen",
      title: isEn
        ? "Fill the composer with an end-to-end showcase prompt (admin only)"
        : "Composer mit einem End-to-End-Showcase-Prompt füllen (nur Admin)",
      notAdmin: isEn ? "Admin access required" : "Admin-Zugriff erforderlich",
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
      '<polygon points="5 3 19 12 5 21 5 3"></polygon>' +
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

  // --- Click handler: fetch prompt and fill composer ------------------------
  var handlersWired = false;

  function wireHandlers(btn) {
    if (handlersWired) return;
    handlersWired = true;
    btn.addEventListener("click", function (e) {
      e.preventDefault();
      e.stopPropagation();
      fillComposer();
    });
    btn.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        e.stopPropagation();
        fillComposer();
      }
    });
  }

  function fillComposer() {
    if (!isAllowed) {
      if (typeof showToast === "function") {
        showToast(localized().notAdmin, "error");
      }
      return;
    }
    fetch("/api/debug-allowed", { credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) throw new Error("status " + r.status);
        return r.json();
      })
      .then(function (data) {
        var prompt = (data && data.prompt) || "";
        if (!prompt) return;
        var textarea = findComposerTextarea();
        if (!textarea) return;
        var nativeSetter = Object.getOwnPropertyDescriptor(
          window.HTMLTextAreaElement.prototype, "value"
        );
        if (nativeSetter && nativeSetter.set) {
          nativeSetter.set.call(textarea, prompt);
        } else {
          textarea.value = prompt;
        }
        textarea.dispatchEvent(new Event("input", { bubbles: true }));
        textarea.focus();
      })
      .catch(function () {
        // Silently ignore — the button is only visible to admins anyway.
      });
  }

  function findComposerTextarea() {
    var textareas = document.querySelectorAll("textarea");
    for (var i = 0; i < textareas.length; i++) {
      var ta = textareas[i];
      if (ta.offsetParent !== null) return ta;
    }
    return textareas.length > 0 ? textareas[0] : null;
  }

  // --- /api/debug-allowed polling (visibility) ------------------------------
  var fetching = false;
  function refresh() {
    if (fetching) return;
    fetching = true;
    fetch("/api/debug-allowed", { credentials: "same-origin" })
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
