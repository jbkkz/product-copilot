// Requivo Web — minimal progressive enhancement. The app works without this file; HTMX (vendored
// locally) drives the partial updates. Here we add a visible loading signal so it's always clear that
// something is happening — both for HTMX swaps (answers, generation) and for full-page form navigations
// (creating a session, running discovery, which otherwise block with no feedback).
(function () {
  "use strict";

  var bar = document.getElementById("progress");
  var timer = null;

  function start() {
    if (!bar) return;
    bar.classList.add("on");
    var w = 12;
    bar.style.width = w + "%";
    clearInterval(timer);
    // ease toward 90% while we wait; the request completing (or the page navigating) finishes it.
    timer = setInterval(function () { w += (90 - w) * 0.12; bar.style.width = w + "%"; }, 300);
  }

  function done() {
    if (!bar) return;
    clearInterval(timer);
    bar.style.width = "100%";
    setTimeout(function () { bar.classList.remove("on"); bar.style.width = "0%"; }, 350);
  }

  function markLoading(scope, on) {
    if (!scope) return;
    var btn = scope.matches && scope.matches("button[type=submit]")
      ? scope
      : (scope.querySelector ? scope.querySelector('button[type="submit"]') : null);
    if (btn) { btn.classList.toggle("loading", on); btn.disabled = on; }
  }

  // HTMX partial requests (answers, artifact generation).
  document.body.addEventListener("htmx:beforeRequest", function (e) { start(); markLoading(e.detail.elt, true); });
  document.body.addEventListener("htmx:afterRequest", function (e) { done(); markLoading(e.detail.elt, false); });

  // Plain full-page form submits (create session, run discovery). HTMX forms carry hx-post and are
  // handled above — skip them here to avoid starting the bar twice.
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (form.getAttribute && form.getAttribute("hx-post") !== null) return;
    start();
    markLoading(form, true);
  });

  // Reset the bar if the user navigates back to a cached page.
  window.addEventListener("pageshow", function () { if (bar) { bar.classList.remove("on"); bar.style.width = "0%"; } });
})();
