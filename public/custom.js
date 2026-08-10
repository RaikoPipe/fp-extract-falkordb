// Custom JS loader — Chainlit's ``custom_js`` config field accepts a
// single file, so this loader injects the KG badge (header indicator) and
// the document-sidebar toggle (floating button) as separate <script>
// elements. Each appended script runs as soon as it loads; because this
// loader itself runs with ``defer`` (the default custom_js_attributes),
// both children execute after DOMContentLoaded, which is what both
// scripts expect (they listen for DOMContentLoaded if it hasn't fired).
//
// Add new custom_js scripts here.
(function () {
  "use strict";
  var sources = ["/public/toast.js", "/public/kg_badge.js", "/public/docs_toggle.js", "/public/graph_view.js", "/public/debug_button.js"];
  for (var i = 0; i < sources.length; i++) {
    var s = document.createElement("script");
    s.src = sources[i];
    s.defer = true;
    document.head.appendChild(s);
  }
})();