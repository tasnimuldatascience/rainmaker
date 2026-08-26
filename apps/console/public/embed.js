/**
 * The tag a customer pastes into their own website.
 *
 *   <script src="https://your-rainmaker/embed.js" data-key="rk_..." defer></script>
 *
 * IT IS AN IFRAME, AND THAT IS THE WHOLE DESIGN. Everything real — the socket, the audio graph,
 * the face, the state — lives on our origin inside a frame. What runs on the customer's page is
 * this file: a button, a frame, and a message listener. Three reasons, in order of how much they
 * would hurt:
 *
 *   ISOLATION. A marketing site is a pile of somebody else's CSS and a tag manager. Injecting a
 *   React tree into it means their `* { box-sizing }` reshapes our layout and our styles leak
 *   into their page. An iframe cannot be reached by either.
 *
 *   SECURITY. Microphone permission, the WebSocket and the audio graph are scoped to OUR origin,
 *   not theirs. Their page never sees the connection and cannot read what a visitor said.
 *
 *   SIZE. This is two kilobytes. The alternative is asking a customer to load a React bundle
 *   onto their landing page, which is the sort of thing that gets an integration removed after
 *   the first Lighthouse report.
 *
 * IT DOES NOT AUTOPLAY. The launcher is a button because a voice starting on its own when
 * somebody lands on a page is the behaviour that gets a product blocked, and because browsers
 * will not give an AudioContext to a page nobody has clicked.
 */
(function () {
  "use strict";

  var script = document.currentScript;
  if (!script) {
    // `currentScript` is null inside a module or when a tag manager re-injects the tag. Fall
    // back to finding ourselves by src, which is the only handle left.
    var all = document.getElementsByTagName("script");
    for (var i = all.length - 1; i >= 0; i -= 1) {
      if (all[i].src && all[i].src.indexOf("embed.js") !== -1) {
        script = all[i];
        break;
      }
    }
  }
  if (!script) return;

  var key = script.getAttribute("data-key") || "";
  var label = script.getAttribute("data-label") || "Talk to us";
  var origin = new URL(script.src, window.location.href).origin;
  var side = script.getAttribute("data-side") === "left" ? "left" : "right";

  if (!key) {
    // Loud in the console, silent on the page. A customer who mis-pastes the tag should find
    // out in devtools, not by putting a broken widget in front of their buyers.
    console.error("[rainmaker] embed.js needs a data-key attribute");
    return;
  }

  var ID = "rainmaker-embed";
  if (document.getElementById(ID)) return; // a tag manager firing twice is normal

  var host = document.createElement("div");
  host.id = ID;
  host.setAttribute("style", [
    "position:fixed",
    "bottom:20px",
    side + ":20px",
    "z-index:2147483000", // below a modal's max, above everything a marketing site does
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif",
  ].join(";"));

  var launcher = document.createElement("button");
  launcher.type = "button";
  launcher.textContent = label;
  launcher.setAttribute("aria-label", label);
  launcher.setAttribute("style", [
    "display:flex",
    "align-items:center",
    "gap:8px",
    "padding:12px 20px",
    "border-radius:999px",
    "border:none",
    "background:#5d8bff",
    "color:#fff",
    "font-size:15px",
    "font-weight:600",
    "cursor:pointer",
    "box-shadow:0 6px 24px rgba(0,0,0,.28)",
  ].join(";"));

  var frame = null;

  function openPanel() {
    if (frame) return;
    frame = document.createElement("iframe");
    frame.src = origin + "/embed.html?key=" + encodeURIComponent(key);
    frame.title = "Talk to our team";
    // The microphone is the point of the product, so the frame is granted it explicitly —
    // a same-origin-only default would silently disable hold-to-talk on every customer site.
    frame.allow = "microphone; autoplay";
    frame.setAttribute("style", [
      "width:min(390px,calc(100vw - 32px))",
      "height:min(620px,calc(100vh - 120px))",
      "border:0",
      "border-radius:16px",
      "box-shadow:0 12px 48px rgba(0,0,0,.35)",
      "background:#0e1014",
      "display:block",
    ].join(";"));
    host.insertBefore(frame, launcher);
    launcher.textContent = "Close";
  }

  function closePanel() {
    if (!frame) return;
    // Removed rather than hidden: a hidden iframe keeps its socket, its microphone permission
    // and its audio graph, and a visitor who closed the widget expects all three to stop.
    host.removeChild(frame);
    frame = null;
    launcher.textContent = label;
  }

  launcher.addEventListener("click", function () {
    if (frame) closePanel();
    else openPanel();
  });

  window.addEventListener("message", function (event) {
    // Only our own frame is listened to. A page hosting this widget also hosts everybody
    // else's scripts, any of which can post a message.
    if (event.origin !== origin || !event.data) return;
    if (event.data.rainmaker === "close") closePanel();
  });

  host.appendChild(launcher);
  document.body.appendChild(host);
})();
