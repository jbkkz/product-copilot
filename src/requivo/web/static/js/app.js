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
    if (btn) btn.classList.toggle("loading", on);
  }

  // One provider call at a time. The documents card offers a button per generator, and every one of
  // them posts to the same region: clicking a second while the first is in flight starts a second paid
  // call whose result the first swap then overwrites — the reader sees one document, was billed for
  // two, and has no way to tell which one they are looking at. Disabling only the clicked button (what
  // this did before) leaves every sibling live, so the rule has to be the page, not the form: while
  // anything is in flight every submit button is muted, and the muting is what tells the reader the
  // page is already working. Nothing here is a safety mechanism — the server holds the revision lock
  // either way, and without JS the page still works. It is the honest reading of what is happening.
  var inFlight = 0;

  function applyBusy() {
    var busy = inFlight > 0;
    document.body.classList.toggle("busy", busy);
    // Queried fresh every time: an HTMX swap replaces the buttons mid-flight, so a saved node list
    // would re-enable elements the document no longer holds and leave the new ones disabled.
    var buttons = document.querySelectorAll('button[type="submit"]');
    for (var i = 0; i < buttons.length; i++) buttons[i].disabled = busy;
  }

  function setBusy(on) {
    inFlight = Math.max(0, inFlight + (on ? 1 : -1));
    applyBusy();
  }

  // HTMX partial requests (answers, artifact generation).
  document.body.addEventListener("htmx:beforeRequest", function (e) {
    start(); markLoading(e.detail.elt, true); setBusy(true);
  });
  document.body.addEventListener("htmx:afterRequest", function (e) {
    done(); markLoading(e.detail.elt, false); setBusy(false);
  });
  // A swap brings in markup that carries no disabled attribute, so re-assert the state over it: with
  // one request in flight that is a no-op, and with two it is the difference between the first one
  // finishing and quietly handing the reader live buttons while the second is still running.
  document.body.addEventListener("htmx:afterSwap", applyBusy);

  // Plain full-page form submits (create session, run discovery). HTMX forms carry hx-post and are
  // handled above — skip them here to avoid starting the bar twice. No matching release: the page is
  // navigating away, and `pageshow` clears the state if the reader comes back to a cached copy.
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (form.getAttribute && form.getAttribute("hx-post") !== null) return;
    start();
    markLoading(form, true);
    setBusy(true);
  });

  // Reset the bar if the user navigates back to a cached page.
  window.addEventListener("pageshow", function () {
    if (bar) { bar.classList.remove("on"); bar.style.width = "0%"; }
    inFlight = 0;
    applyBusy();
  });
})();
