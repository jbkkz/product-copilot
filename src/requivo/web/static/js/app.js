// Requivo Web — minimal progressive enhancement. The app works without this file; HTMX (loaded
// separately, vendored locally) drives the partial updates. Here we only add small conveniences.
(function () {
  "use strict";

  // Disable a submit button while its HTMX request is in flight, so a slow provider call can't be
  // double-submitted. HTMX toggles the .htmx-request class on the triggering element.
  document.body.addEventListener("htmx:beforeRequest", function (e) {
    var btn = e.detail.elt.querySelector('button[type="submit"], input[type="submit"]');
    if (btn) { btn.disabled = true; }
  });
  document.body.addEventListener("htmx:afterRequest", function (e) {
    var btn = e.detail.elt.querySelector('button[type="submit"], input[type="submit"]');
    if (btn) { btn.disabled = false; }
  });
})();
