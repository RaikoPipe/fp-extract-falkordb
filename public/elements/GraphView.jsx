// GraphView — a CustomElement that renders a knowledge-graph snapshot as an
// inline Cytoscape canvas inside the chat timeline. Attached to the
// assistant message after an ingestion run (extract_and_write / Ingest
// button) via _collect_visual_elements, so the user gets a persistent,
// scroll-back-able record of the post-ingestion topology alongside the
// streamed text. The live floating panel (public/graph_view.js) is the
// real-time view; this element is the snapshot pinned to the message.
//
// Cytoscape is loaded lazily from the same CDN URL as graph_view.js so the
// two never load two copies. The element is a dumb view: the Python side
// serializes the snapshot (build_graph_snapshot) into props; this file does
// no fetching.
import { useEffect, useRef, useState } from "react"

var CYTO_URL =
  "https://cdn.jsdelivr.net/npm/cytoscape@3.28.1/dist/cytoscape.min.js"

var cytoPromise = null
function loadCytoscape() {
  if (window.cytoscape) return Promise.resolve()
  if (cytoPromise) return cytoPromise
  cytoPromise = new Promise(function (resolve, reject) {
    var s = document.createElement("script")
    s.src = CYTO_URL
    s.defer = true
    s.onload = function () { resolve() }
    s.onerror = function () { reject(new Error("cytoscape load failed")) }
    document.head.appendChild(s)
  })
  return cytoPromise
}

function formatFooter(stats, truncated, lang) {
  if (!stats) return ""
  var parts = []
  parts.push((stats.node_count || 0) + (lang === "en" ? " nodes" : " Knoten"))
  parts.push((stats.edge_count || 0) + (lang === "en" ? " edges" : " Kanten"))
  if (stats.truncated || truncated) {
    var n = (stats.node_count || 0) > 500 ? 500 : (stats.node_count || 0)
    parts.push(
      lang === "en" ? "showing first " + n : "zeige erste " + n
    )
  }
  return parts.join(" · ")
}

export default function GraphView() {
  var nodes = props.nodes || []
  var edges = props.edges || []
  var stats = props.stats || {}
  var graph = props.graph || ""
  var lang = props.lang === "en" ? "en" : "de"
  var title = props.title || (lang === "en" ? "Knowledge graph" : "Wissensgraph")
  var containerRef = useRef(null)
  var cyRef = useRef(null)
  var [loadError, setLoadError] = useState(false)

  useEffect(function () {
    if (cyRef.current || loadError) return
    var cancelled = false
    loadCytoscape()
      .then(function () {
        if (cancelled || !containerRef.current) return
        if (cyRef.current) return
        cyRef.current = window.cytoscape({
          container: containerRef.current,
          elements: [],
          style: [
            {
              selector: "node",
              style: {
                label: "data(label)",
                "text-valign": "center",
                "text-halign": "center",
                "font-size": "9px",
                "background-color": "#6366f1",
                color: "#fff",
                width: 30,
                height: 30,
                "text-wrap": "wrap",
                "text-max-width": 50,
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
                width: 1.5,
                "line-color": "#a1a1aa",
                "target-arrow-color": "#a1a1aa",
                "target-arrow-shape": "triangle",
                "curve-style": "bezier",
                label: "data(type)",
                "font-size": "7px",
                "text-rotation": "autorotate",
                color: "#71717a",
              },
            },
          ],
          layout: { name: "cose", animate: false },
        })
        renderElements(cyRef.current, nodes, edges)
      })
      .catch(function () {
        if (!cancelled) setLoadError(true)
      })
    return function () { cancelled = true }
  }, [])

  // Re-render when the props change (a new snapshot is attached).
  useEffect(function () {
    if (cyRef.current) renderElements(cyRef.current, nodes, edges)
  }, [nodes, edges])

  function renderElements(cy, nodes, edges) {
    cy.elements().remove()
    var cyNodes = nodes.map(function (n) {
      var label = (n._labels && n._labels[0]) || "Resource"
      return { data: { id: n.name, label: label, name: n.name, props: n } }
    })
    var cyEdges = edges.map(function (e, i) {
      return {
        data: {
          id: e.source + "|" + e.target + "|" + e.type + "|" + i,
          source: e.source,
          target: e.target,
          type: e.type,
        },
      }
    })
    cy.add(cyNodes.concat(cyEdges))
    if (cy.nodes().length > 0) {
      cy.layout({
        name: "cose",
        animate: false,
        nodeRepulsion: function () { return 8000 },
        idealEdgeLength: function () { return 80 },
        padding: 10,
      }).run()
    }
  }

  if (loadError) {
    return (
      <div style={{
        border: "1px solid rgb(228,228,231)",
        borderRadius: 8,
        padding: 12,
        fontSize: 12,
        color: "rgb(82,82,91)",
      }}>
        {lang === "en" ? "Graph view unavailable (Cytoscape failed to load)." :
          "Graph-Anzeige nicht verfügbar (Cytoscape konnte nicht geladen werden.)"}
      </div>
    )
  }

  if (!nodes.length) {
    return (
      <div style={{
        border: "1px solid rgb(228,228,231)",
        borderRadius: 8,
        padding: 12,
        fontSize: 12,
        color: "rgb(82,82,91)",
      }}>
        {lang === "en" ? "No nodes in graph yet." : "Noch keine Knoten im Graph."}
      </div>
    )
  }

  return (
    <div style={{
      border: "1px solid rgb(228,228,231)",
      borderRadius: 8,
      overflow: "hidden",
      marginTop: 8,
    }}>
      <div style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "6px 10px",
        borderBottom: "1px solid rgb(228,228,231)",
        fontSize: 12,
        fontWeight: 600,
      }}>
        <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {title}{graph ? " — " + graph : ""}
        </span>
      </div>
      <div ref={containerRef} style={{
        height: 280,
        background: "rgb(250,250,250)",
        minHeight: 0,
      }} />
      <div style={{
        padding: "4px 10px",
        borderTop: "1px solid rgb(228,228,231)",
        fontSize: 10,
        color: "rgb(113,113,122)",
      }}>
        {formatFooter(stats, false, lang)}
      </div>
    </div>
  )
}