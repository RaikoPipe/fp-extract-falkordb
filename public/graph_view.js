// Knowledge-graph topology viewer — a floating side panel that renders the
// live graph from /api/graph-snapshot. Mirrors the docs_toggle.js pattern
// (vanilla JS, no Chainlit message, safe in starter + chat views). Polls the
// authenticated endpoint, draws nodes + edges with Cytoscape (loaded once from
// a CDN as an ESM import — no build step, no Python dep), and refreshes at
// an adaptive cadence: 15s when idle, ~2s while an ingestion tool Step is
// visible in the chat timeline (detected by scanning the DOM for a Step whose
// name contains "extract_and_write" — the agent's ingestion tool).
//
// Why a floating panel and not a cl.CustomElement: the panel must persist
// across the starter → chat view transition and survive chat re-renders
// without sending a Chainlit message (which would dismiss the starter
// screen). CustomElement chat messages are tied to a message and re-render
// only inside the chat view; a fixed-position DOM overlay is the only shape
// that satisfies both constraints (same conclusion the KG badge reached).
//
// Cytoscape is loaded lazily on first open so the initial page load pays
// nothing; the ~300 KB library is fetched once and cached by the browser.
// Falls back gracefully: a 401 hides the panel, a 503 / fetch error leaves
// the last successful render in place and surfaces a small "unavailable"
// badge. The panel never blocks the chat — it is a side panel with its own
// scroll and pointer events.
(function () {
  "use strict";

  var PANEL_ID = "kg-view";
  var TOGGLE_BTN_ID = "kg-view-toggle";
  var IDLE_POLL_MS = 15000;
  var ACTIVE_POLL_MS = 2000;
  var CYTO_URL =
    "https://cdn.jsdelivr.net/npm/cytoscape@3.28.1/dist/cytoscape.min.js";
  var INGESTION_STEP_MARKER = "extract_and_write";

  var cy = null; // Cytoscape instance, built on first open.
  var cytoLoaded = false; // Script tag inserted + resolved.
  var cytoLoading = null; // In-flight load promise (dedupes concurrent opens).
  var panelEl = null;
  var toggleBtnEl = null;
  var isOpen = false;
  var lastSnapshot = null;
  var fetching = false;
  var currentTimer = null;

  function localized() {
    var htmlLang = (document.documentElement.lang || "de").toLowerCase();
    var isEn = htmlLang.indexOf("en") === 0;
    return {
      open: isEn ? "Graph" : "Graph",
      close: isEn ? "Close" : "Schließen",
      openTitle: isEn ? "Open knowledge graph viewer" : "Wissensgraph-Anzeige öffnen",
      closeTitle: isEn ? "Close knowledge graph viewer" : "Wissensgraph-Anzeige schließen",
      empty: isEn ? "No graph selected" : "Kein Graph ausgewählt",
      unavailable: isEn ? "Graph unavailable" : "Graph nicht verfügbar",
      truncated: isEn ? "Showing first N nodes" : "Zeige erste N Knoten",
      nodes: isEn ? "nodes" : "Knoten",
      edges: isEn ? "edges" : "Kanten",
      pause: isEn ? "Pause" : "Pause",
      resume: isEn ? "Resume" : "Fortsetzen",
      falkorUi: isEn ? "Open in FalkorDB UI" : "In FalkorDB UI öffnen",
    };
  }

  // --- Cytoscape lazy loader ----------------------------------------------

  function loadCytoscape() {
    if (cytoLoaded) return Promise.resolve();
    if (cytoLoading) return cytoLoading;
    cytoLoading = new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.src = CYTO_URL;
      s.defer = true;
      s.onload = function () {
        cytoLoaded = true;
        resolve();
      };
      s.onerror = function () {
        cytoLoading = null;
        reject(new Error("cytoscape load failed"));
      };
      document.head.appendChild(s);
    });
    return cytoLoading;
  }

  // --- Panel + toggle button DOM -----------------------------------------

  function ensureToggle() {
    var existing = document.getElementById(TOGGLE_BTN_ID);
    if (existing) return existing;
    var btn = document.createElement("div");
    btn.id = TOGGLE_BTN_ID;
    btn.style.cssText = [
      "position:fixed",
      "bottom:1rem",
      "right:8rem", // left of the docs toggle (right:1rem)
      "z-index:9999",
      "display:none", // hidden until the first snapshot lands
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
      toggleOpen();
    });
    btn.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        e.stopPropagation();
        toggleOpen();
      }
    });
    document.body.appendChild(btn);
    return btn;
  }

  function ensurePanel() {
    var existing = document.getElementById(PANEL_ID);
    if (existing) return existing;
    var panel = document.createElement("div");
    panel.id = PANEL_ID;
    panel.style.cssText = [
      "position:fixed",
      "top:4rem",
      "right:1rem",
      "width:420px",
      "height:calc(100vh - 6rem)",
      "z-index:9998",
      "display:none", // hidden until first open
      "flex-direction:column",
      "border:1px solid rgb(228,228,231)",
      "border-radius:12px",
      "background:rgb(255,255,255)",
      "box-shadow:0 4px 16px rgba(0,0,0,0.15)",
      "overflow:hidden",
    ].join(";");
    panel.innerHTML =
      '<div style="display:flex;align-items:center;gap:8px;padding:8px 12px;' +
      'border-bottom:1px solid rgb(228,228,231)">' +
      '<span id="kg-view-title" style="font-weight:600;font-size:13px;flex:1;' +
      'overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span>' +
      '<button id="kg-view-pause" type="button" title="' + localized().pause +
      '" style="border:none;background:transparent;cursor:pointer;padding:4px;' +
      'border-radius:4px;color:inherit"></button>' +
      '<button id="kg-view-close" type="button" title="' + localized().closeTitle +
      '" style="border:none;background:transparent;cursor:pointer;padding:4px;' +
      'border-radius:4px;color:inherit;font-size:16px;line-height:1">x</button>' +
      '</div>' +
      '<div id="kg-view-cy" style="flex:1;min-height:0;background:rgb(250,250,250)">' +
      '</div>' +
      '<div id="kg-view-footer" style="padding:6px 12px;border-top:1px solid ' +
      'rgb(228,228,231);font-size:11px;color:rgb(82,82,91);display:flex;gap:8px;' +
      'align-items:center;justify-content:space-between"></div>';
    document.body.appendChild(panel);
    document.getElementById("kg-view-close").addEventListener("click", function (e) {
      e.preventDefault();
      setOpen(false);
    });
    document.getElementById("kg-view-pause").addEventListener("click", function (e) {
      e.preventDefault();
      togglePaused();
    });
    return panel;
  }

  // --- Open / close + paused state ----------------------------------------

  var paused = false;

  function toggleOpen() {
    setOpen(!isOpen);
  }

  function setOpen(open) {
    isOpen = open;
    if (!panelEl) panelEl = ensurePanel();
    panelEl.style.display = open ? "flex" : "none";
    if (open) {
      // Lazy-load Cytoscape + build the instance on first open.
      loadCytoscape()
        .then(function () {
          if (!cy && window.cytoscape) {
            cy = window.cytoscape({
              container: document.getElementById("kg-view-cy"),
              elements: [],
              style: [
                {
                  selector: "node",
                  style: {
                    "label": "data(label)",
                    "text-valign": "center",
                    "text-halign": "center",
                    "font-size": "10px",
                    "background-color": "#6366f1",
                    "color": "#fff",
                    "width": 36,
                    "height": 36,
                    "text-wrap": "wrap",
                    "text-max-width": 60,
                  },
                },
                {
                  selector: 'node[label = "Resource"]',
                  style: { "background-color": "#6366f1" },
                },
                {
                  selector: 'node[label = "Machine"]',
                  style: { "background-color": "#10b981" },
                },
                {
                  selector: 'node[label = "Line"]',
                  style: { "background-color": "#f59e0b" },
                },
                {
                  selector: 'node[label = "Order"]',
                  style: { "background-color": "#ef4444" },
                },
                {
                  selector: "edge",
                  style: {
                    "width": 1.5,
                    "line-color": "#a1a1aa",
                    "target-arrow-color": "#a1a1aa",
                    "target-arrow-shape": "triangle",
                    "curve-style": "bezier",
                    "label": "data(type)",
                    "font-size": "8px",
                    "text-rotation": "autorotate",
                    "color": "#71717a",
                  },
                },
              ],
              layout: { name: "cose", animate: false },
            });
          }
          if (lastSnapshot) renderSnapshot(lastSnapshot);
          scheduleNext(0); // immediate refresh on open
        })
        .catch(function (err) {
          setFooter(localized().unavailable + " (" + err.message + ")");
        });
    }
    syncToggleLabel();
  }

  function togglePaused() {
    paused = !paused;
    var btn = document.getElementById("kg-view-pause");
    if (btn) {
      btn.textContent = paused ? "▶" : "❚❚";
      btn.title = paused ? localized().resume : localized().pause;
    }
    if (!paused) scheduleNext(0);
  }

  function syncToggleLabel() {
    if (!toggleBtnEl) return;
    var labels = localized();
    var span = toggleBtnEl.querySelector("span");
    if (span) span.textContent = labels.open;
    toggleBtnEl.title = labels.openTitle;
  }

  // --- Rendering ----------------------------------------------------------

  function renderSnapshot(data) {
    if (!cy) return;
    var labels = localized();
    // Title: graph name + node/edge counts.
    var title = document.getElementById("kg-view-title");
    if (title) {
      var name = (data && data.graph) || labels.empty;
      var stats = (data && data.stats) || {};
      var nodeCount = stats.node_count || 0;
      var edgeCount = stats.edge_count || 0;
      title.textContent = name + "  ·  " + nodeCount + " " + labels.nodes +
        " / " + edgeCount + " " + labels.edges;
    }

    // Empty / error states: clear the canvas.
    if (!data || !data.nodes || !data.nodes.length) {
      cy.elements().remove();
      if (data && data.stats && data.error === "graph-unavailable") {
        setFooter(labels.unavailable);
      } else if (data && !data.graph) {
        setFooter(labels.empty);
      }
      return;
    }

    // Build Cytoscape elements: nodes keyed by name (stable id across polls
    // so the layout doesn't reflow on every refresh), edges keyed by
    // src|tgt|type.
    var nodes = data.nodes.map(function (n) {
      var label = (n._labels && n._labels[0]) || "Resource";
      return {
        data: {
          id: n.name,
          label: label,
          name: n.name,
          props: n,
        },
      };
    });
    var edges = data.edges.map(function (e, i) {
      return {
        data: {
          id: e.source + "|" + e.target + "|" + e.type + "|" + i,
          source: e.source,
          target: e.target,
          type: e.type,
        },
      };
    });

    // Diff-friendly update: remove elements no longer present, add new ones,
    // keep positions for survivors so the layout is stable across polls.
    var survivingIds = new Set(nodes.map(function (n) { return n.data.id; }));
    cy.nodes().forEach(function (n) {
      if (!survivingIds.has(n.id())) n.remove();
    });
    nodes.forEach(function (n) {
      if (!cy.getElementById(n.data.id).length) cy.add(n);
      else cy.getElementById(n.data.id).data(n.data);
    });
    var survivingEdgeIds = new Set(edges.map(function (e) { return e.data.id; }));
    cy.edges().forEach(function (e) {
      if (!survivingEdgeIds.has(e.id())) e.remove();
    });
    edges.forEach(function (e) {
      if (!cy.getElementById(e.data.id).length) cy.add(e);
    });

    // Run a cose layout only when the graph grew (new nodes added) — avoids
    // a full reflow on every idle poll when nothing changed.
    if (cy.nodes().length > 0 && data.stats && data.stats.node_count > 0) {
      var addedAny = nodes.some(function (n) {
        return cy.getElementById(n.data.id).length &&
          !cy.getElementById(n.data.id).position();
      });
      // Always run layout on the first render (positions undefined) and
      // when the node count grew since the last render.
      if (!lastSnapshot || lastSnapshot.stats.node_count < data.stats.node_count) {
        runLayout();
      }
    }

    // Footer: truncation notice + FalkorDB UI link.
    var footerParts = [];
    if (data.stats && data.stats.truncated) {
      footerParts.push(labels.truncated.replace("N", String(data.nodes.length)));
    }
    setFooter(footerParts.join(" · "));
  }

  function runLayout() {
    if (!cy || cy.nodes().length === 0) return;
    cy.layout({
      name: "cose",
      animate: false,
      nodeRepulsion: function () { return 8000; },
      idealEdgeLength: function () { return 80; },
      padding: 10,
    }).run();
  }

  function setFooter(text) {
    var footer = document.getElementById("kg-view-footer");
    if (!footer) return;
    // Preserve the FalkorDB UI link (right side) if present.
    var link = footer.querySelector("a");
    while (footer.firstChild && footer.firstChild !== link) {
      footer.removeChild(footer.firstChild);
    }
    if (text) {
      var span = document.createElement("span");
      span.textContent = text;
      footer.insertBefore(span, link);
    }
    // Add the FalkorDB UI link once.
    if (!link) {
      link = document.createElement("a");
      link.textContent = localized().falkorUi;
      link.href = window.location.protocol + "//" + window.location.hostname + ":3000";
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.style.cssText = "color:rgb(99,102,241);text-decoration:none";
      footer.appendChild(link);
    }
  }

  // --- Polling ------------------------------------------------------------

  function isInjectionActive() {
    // Detect a visible "extract_and_write" Step in the chat timeline. The
    // agent's tool Step panels carry the tool name in their header; we scan
    // for a Step whose text content includes the marker. This is a heuristic
    // but cheap and stable across Chainlit re-renders.
    var steps = document.querySelectorAll("[class*='step'], [class*='Step']");
    for (var i = 0; i < steps.length; i++) {
      if (steps[i].textContent &&
          steps[i].textContent.indexOf(INGESTION_STEP_MARKER) !== -1) {
        return true;
      }
    }
    // The Ingest action button path emits a TaskList panel; detect it too.
    var tasklist = document.querySelector("[class*='task'], [class*='Task']");
    if (tasklist && tasklist.textContent &&
        tasklist.textContent.toLowerCase().indexOf("ingest") !== -1) {
      return true;
    }
    return false;
  }

  function nextDelay() {
    return isInjectionActive() ? ACTIVE_POLL_MS : IDLE_POLL_MS;
  }

  function scheduleNext(delayOverride) {
    if (currentTimer) {
      clearTimeout(currentTimer);
      currentTimer = null;
    }
    var delay = typeof delayOverride === "number" ? delayOverride : nextDelay();
    currentTimer = setTimeout(refresh, delay);
  }

  function refresh() {
    currentTimer = null;
    if (paused || fetching) {
      scheduleNext();
      return;
    }
    fetching = true;
    fetch("/api/graph-snapshot", { credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) throw new Error("status " + r.status);
        return r.json();
      })
      .then(function (data) {
        lastSnapshot = data;
        // Show the toggle button once the first non-empty snapshot lands.
        if (data && data.graph && !toggleBtnEl) {
          toggleBtnEl = ensureToggle();
          toggleBtnEl.style.display = "flex";
        }
        if (toggleBtnEl && data && data.graph) {
          toggleBtnEl.style.display = "flex";
        }
        if (isOpen) renderSnapshot(data);
      })
      .catch(function (err) {
        // 401 -> not logged in; hide the toggle silently.
        if (err && err.message && err.message.indexOf("401") !== -1) {
          if (toggleBtnEl) toggleBtnEl.style.display = "none";
          return;
        }
        // Other errors: leave the last render in place, surface a footer note.
        if (isOpen) setFooter(localized().unavailable);
      })
      .finally(function () {
        fetching = false;
        scheduleNext();
      });
  }

  // --- Bootstrap ----------------------------------------------------------

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  function init() {
    // First fetch on load to decide whether to show the toggle button. We
    // do not open the panel automatically — the user opens it via the
    // toggle. The poll then keeps the toggle visible + the panel (if open)
    // refreshed.
    refresh();
  }

  // Re-render on DOM mutations (Chainlit re-renders on view transitions
  // such as starter -> chat). Throttle with a flag so a burst of mutations
  // (e.g. streaming tokens) doesn't spam the endpoint.
  var dirty = false;
  if (typeof MutationObserver !== "undefined") {
    var observer = new MutationObserver(function () {
      if (dirty) return;
      dirty = true;
      setTimeout(function () {
        dirty = false;
        // Only re-evaluate the toggle's visibility; a full refresh is
        // unnecessary on every DOM mutation (the poll handles data).
        if (lastSnapshot && lastSnapshot.graph && toggleBtnEl) {
          toggleBtnEl.style.display = "flex";
        }
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