// AgentTodos portal script — moves the rendered AgentTodos CustomElement
// card into the Chainlit composer (``#message-composer``, a stable id in
// Chainlit 2.x) while the run is active, then restores it to its original
// in-chat-flow position when the run concludes so the concluded stages
// remain as a chronological record. Does NOT clone the node (React owns
// it); moves the same DOM node, preserving Chainlit's React-tree
// expectations.
//
// ``data-active`` on the card is the authoritative signal, driven by the
// ``active`` prop (todos non-empty OR ingestion_running). When true the
// card is portaled above the composer; when false it is returned to its
// home slot (the message wrapper that originally hosted it).
//
// The empty stub render (``!hasTodos && !hasStages``) keeps a stable DOM
// node in the home slot so Chainlit's teardown never hits
// ``Node.removeChild`` on a missing child. The stub itself is never
// portaled — only the populated card is.
//
// Fallback: if ``#message-composer`` is absent (custom Chainlit skin or
// a future rename), the card stays in-chat-flow — graceful degradation
// to the pre-portal behavior, no error thrown.
(function () {
  "use strict";

  var CARD_ID = "agent-todos-card";
  var COMPOSER_ID = "message-composer"; // Chainlit internal id; stable in 2.x
  var OBSERVE_DEBOUNCE_MS = 200;

  var observeDirty = false;
  // Home-slot refs captured at portal time so we can restore the card to
  // its original position. ``homeParent`` is the message wrapper that
  // hosts the element; ``homeNext`` is the node the card should precede
  // on restore (null = append to end of parent).
  var portaled = false;
  var homeParent = null;
  var homeNext = null;

  function findCard() {
    return document.getElementById(CARD_ID);
  }

  function findComposer() {
    return document.getElementById(COMPOSER_ID);
  }

  function cardHasContent(card) {
    if (!card) return false;
    var text = (card.textContent || "").trim();
    return text.length > 0;
  }

  function isActive(card) {
    if (!card) return false;
    return card.getAttribute("data-active") === "true";
  }

  function portalToComposer(card, composer) {
    if (portaled) return;
    homeParent = card.parentNode;
    homeNext = card.nextSibling;
    // Insert as the first child of the composer so the panel sits above
    // the conditional "selected command" bar and the textarea.
    if (composer.firstChild) {
      composer.insertBefore(card, composer.firstChild);
    } else {
      composer.appendChild(card);
    }
    card.style.zIndex = "50";
    card.style.maxHeight = "40vh";
    card.style.overflow = "auto";
    card.style.marginBottom = "4px";
    portaled = true;
  }

  function restoreToHome(card) {
    if (!portaled) return;
    if (homeParent) {
      if (homeNext && homeNext.parentNode === homeParent) {
        homeParent.insertBefore(card, homeNext);
      } else {
        homeParent.appendChild(card);
      }
    }
    card.style.zIndex = "";
    card.style.maxHeight = "";
    card.style.overflow = "";
    card.style.marginBottom = "";
    portaled = false;
    homeParent = null;
    homeNext = null;
  }

  function syncCard() {
    var card = findCard();
    if (!card) return;
    var shouldPortal = isActive(card) && cardHasContent(card);
    if (shouldPortal && !portaled) {
      var composer = findComposer();
      if (!composer) return; // graceful degradation — stay in chat flow
      portalToComposer(card, composer);
    } else if (!shouldPortal && portaled) {
      restoreToHome(card);
    }
  }

  function scheduleSync() {
    if (observeDirty) return;
    observeDirty = true;
    setTimeout(function () {
      observeDirty = false;
      syncCard();
    }, OBSERVE_DEBOUNCE_MS);
  }

  // Bootstrap
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      syncCard();
    });
  } else {
    syncCard();
  }

  // Watch for DOM changes (new messages, element renders, prop updates,
  // data-active flips). The MutationObserver is what drives both the
  // initial portal and the restore-on-conclude.
  if (typeof MutationObserver !== "undefined") {
    var observer = new MutationObserver(function () {
      scheduleSync();
    });
    var startObserving = function () {
      var target = document.body || document.documentElement;
      if (target) observer.observe(target, { childList: true, subtree: true, attributes: true, attributeFilter: ["data-active"] });
    };
    if (document.body) startObserving();
    else document.addEventListener("DOMContentLoaded", startObserving);
  }

  // Re-measure on resize (composer height changes, mobile keyboard)
  window.addEventListener("resize", function () {
    scheduleSync();
  });
})();