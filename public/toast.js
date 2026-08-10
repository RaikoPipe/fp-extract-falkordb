// Lightweight vanilla-JS toast notification utility.
// Renders a fixed-position notification at the top-center, animates in,
// and auto-dismisses after ~4 seconds. Supports "error" (red) and "info"
// (blue) types. Exposes a single global: window.showToast(message, type).
//
// Usage from any custom_js script:
//   showToast("No documents available", "error");
//   showToast("Graph refreshed", "info");
(function () {
  "use strict";

  var TOAST_DURATION_MS = 4000;
  var TOAST_ID = "kg-toast";

  function localized() {
    var htmlLang = (document.documentElement.lang || "de").toLowerCase();
    var isEn = htmlLang.indexOf("en") === 0;
    return {
      dismiss: isEn ? "Dismiss" : "Schließen",
    };
  }

  function showToast(message, type) {
    type = type || "error";
    var existing = document.getElementById(TOAST_ID);
    if (existing) existing.remove();

    var toast = document.createElement("div");
    toast.id = TOAST_ID;
    toast.setAttribute("role", "alert");
    toast.setAttribute("aria-live", "assertive");

    var bg = type === "error" ? "rgb(239,68,68)" : "rgb(59,130,246)";
    toast.style.cssText = [
      "position:fixed",
      "top:1rem",
      "left:50%",
      "transform:translateX(-50%)",
      "z-index:99999",
      "display:flex",
      "align-items:center",
      "gap:8px",
      "padding:10px 16px",
      "border-radius:8px",
      "font-size:14px",
      "font-weight:500",
      "line-height:1.3",
      "color:#fff",
      "background:" + bg,
      "box-shadow:0 4px 12px rgba(0,0,0,0.25)",
      "opacity:0",
      "transition:opacity 0.2s ease",
      "max-width:420px",
      "text-align:center",
    ].join(";");

    var msg = document.createElement("span");
    msg.textContent = message;
    msg.style.cssText = "flex:1;overflow:hidden;text-overflow:ellipsis";
    toast.appendChild(msg);

    var dismiss = document.createElement("button");
    dismiss.textContent = "×";
    dismiss.title = localized().dismiss;
    dismiss.style.cssText = [
      "flex:0 0 auto",
      "border:none",
      "background:transparent",
      "color:inherit",
      "cursor:pointer",
      "font-size:18px",
      "line-height:1",
      "padding:0 2px",
      "opacity:0.8",
    ].join(";");
    dismiss.addEventListener("click", function () { removeToast(toast); });
    toast.appendChild(dismiss);

    document.body.appendChild(toast);

    // Animate in.
    requestAnimationFrame(function () {
      toast.style.opacity = "1";
    });

    // Auto-dismiss.
    var timer = setTimeout(function () { removeToast(toast); }, TOAST_DURATION_MS);
    toast._dismissTimer = timer;
  }

  function removeToast(toast) {
    if (!toast || !toast.parentElement) return;
    if (toast._dismissTimer) clearTimeout(toast._dismissTimer);
    toast.style.opacity = "0";
    setTimeout(function () {
      if (toast.parentElement) toast.parentElement.removeChild(toast);
    }, 200);
  }

  window.showToast = showToast;
})();
