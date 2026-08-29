// A minimal DOM, enough to execute `web/static/js/app.js` for real and watch what it decides to do
// with an error response. Driven by `test_error_responses_are_swapped_into_the_page_rather_than_dropped`.
//
// Why this exists: the rule under test is browser behaviour (#203). The vendored htmx swaps only
// 200-399, so every 4xx/5xx fragment this app builds was invisible — and the Python suite drives a
// `TestClient`, which runs no JavaScript, so it asserts response bodies the browser never rendered.
// The alternative was asserting that a literal string appears in the asset, which pins the
// implementation's spelling rather than its effect and would pass against code that swaps nothing.
//
// This models the one decision htmx delegates: it fires `htmx:beforeSwap` with `detail.shouldSwap`
// preset from the status, and swaps only if that is still true afterwards. So the harness sets
// `shouldSwap` exactly as htmx 1.9.12 does (`status >= 200 && status < 400 && status !== 204`),
// dispatches, and reports what came back. A regression that removes the listener leaves the 4xx/5xx
// rows false, which is the state this test exists to catch.
//
// Sibling of `busy_harness.js` and deliberately the same shape: tiny, strict, and reporting a
// timeline rather than an assertion, so the Python side owns what the observations mean.
"use strict";

var fs = require("fs");
var appJsPath = process.argv[2];
var src = fs.readFileSync(appJsPath, "utf8");

var SUBMIT_SELECTOR = 'button[type="submit"]';

function makeButton() {
  return {
    disabled: false,
    classList: { toggle: function () {} },
    matches: function (sel) { return sel.indexOf("submit") !== -1; },
    querySelector: function () { return null; }
  };
}
var buttons = [makeButton()];

var listeners = {};
function on(type, fn) { (listeners[type] = listeners[type] || []).push(fn); }
function fire(type, ev) {
  var fns = listeners[type] || [];
  for (var i = 0; i < fns.length; i++) fns[i](ev);
  return ev;
}

var documentStub = {
  body: {
    classList: { toggle: function () {} },
    addEventListener: on
  },
  getElementById: function () { return null; },
  querySelectorAll: function (sel) {
    if (sel !== SUBMIT_SELECTOR) {
      throw new Error("the harness only models " + SUBMIT_SELECTOR + ", got: " + sel);
    }
    return buttons;
  },
  addEventListener: on
};
var windowStub = { addEventListener: on };

new Function("document", "window", src)(documentStub, windowStub);

// htmx 1.9.12's own gate, copied from the minified source it ships:
//   var i = f.status >= 200 && f.status < 400 && f.status !== 204
// It is what `detail.shouldSwap` arrives holding, and what a page with no listener keeps.
function htmxDefaultShouldSwap(status) {
  return status >= 200 && status < 400 && status !== 204;
}

function swapDecision(status) {
  var detail = {
    xhr: { status: status },
    shouldSwap: htmxDefaultShouldSwap(status),
    isError: !htmxDefaultShouldSwap(status),
    elt: buttons[0]
  };
  var before = detail.shouldSwap;
  fire("htmx:beforeSwap", { detail: detail });
  return {
    status: status,
    htmxWouldSwap: before,        // what htmx decided on its own
    swapped: detail.shouldSwap,   // what the page decided after our listener
    isError: detail.isError
  };
}

// 200 is the control: it must stay swappable, and nothing here may turn it off.
// 409 revision conflict · 413 oversized answers · 502 provider failure on a paid generation —
// the three the issue names, one per route that can produce them.
var statuses = [200, 204, 400, 403, 409, 413, 500, 502];
var log = statuses.map(swapDecision);

process.stdout.write(JSON.stringify(log));
