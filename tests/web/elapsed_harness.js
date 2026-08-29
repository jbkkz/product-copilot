// A minimal DOM plus a fake clock, enough to execute `web/static/js/app.js` for real and watch what
// the status text does while a paid call is in flight. Driven by
// `test_a_long_call_says_so_after_ten_seconds_rather_than_looking_stuck`.
//
// Why this exists: the rule under test is browser behaviour over *time* (#236). The page blocks on a
// synchronous provider call that this repo's own invariants describe as taking "seconds to minutes",
// while the only status text was a static label — so a first-time user whose stated expectation had
// expired concluded it had hung. A `TestClient` runs no JavaScript and has no clock, so the
// alternative was asserting that a literal string appears in the asset, which pins the spelling
// rather than the effect and passes just as well against code that never updates anything.
//
// Sibling of `busy_harness.js` and `error_swap_harness.js`, deliberately the same shape: tiny,
// strict, and reporting a timeline rather than an assertion, so the Python side owns what the
// observations mean. The one addition is the clock: `Date` and the window timers are injected, so
// the ten-second boundary is asserted exactly instead of waited for.
"use strict";

var fs = require("fs");
var appJsPath = process.argv[2];
var src = fs.readFileSync(appJsPath, "utf8");

var SUBMIT_SELECTOR = 'button[type="submit"]';
var SPINNER_SELECTOR = ".spinner";

// The labels the shipped templates actually carry, so the harness reports what a reader sees rather
// than a placeholder.
var LABELS = ["Reading the request…", "Working through the impact…"];

function makeSpinner(label) {
  var attrs = {};
  return {
    textContent: label,
    getAttribute: function (name) {
      return Object.prototype.hasOwnProperty.call(attrs, name) ? attrs[name] : null;
    },
    setAttribute: function (name, value) { attrs[name] = value; }
  };
}

function makeButton() {
  return {
    disabled: false,
    classList: { toggle: function () {} },
    matches: function (sel) { return sel.indexOf("submit") !== -1; },
    querySelector: function () { return null; }
  };
}

var spinners = LABELS.map(makeSpinner);
var buttons = [makeButton()];

var listeners = {};
function on(type, fn) { (listeners[type] = listeners[type] || []).push(fn); }
function fire(type, ev) {
  var fns = listeners[type] || [];
  for (var i = 0; i < fns.length; i++) fns[i](ev);
}

var documentStub = {
  body: {
    classList: { toggle: function () {} },
    addEventListener: on
  },
  getElementById: function () { return null; },
  querySelectorAll: function (sel) {
    if (sel === SUBMIT_SELECTOR) return buttons;
    if (sel === SPINNER_SELECTOR) return spinners;
    throw new Error("the harness models " + SUBMIT_SELECTOR + " and " + SPINNER_SELECTOR
      + ", got: " + sel);
  },
  addEventListener: on
};

// The fake clock. Every interval the page schedules is held here and fired by `advance`, so a tick
// happens exactly when the page asked for one and never otherwise — a real timer would make the
// ten-second boundary a race, and a flaky assertion about a trust signal is worse than none.
var now = 1000000;
var nextTimerId = 1;
var timers = {};

var windowStub = {
  addEventListener: on,
  setInterval: function (fn, ms) {
    timers[nextTimerId] = { fn: fn, every: ms, due: now + ms };
    return nextTimerId++;
  },
  clearInterval: function (id) { delete timers[id]; },
  setTimeout: function (fn, ms) {
    timers[nextTimerId] = { fn: fn, every: null, due: now + ms };
    return nextTimerId++;
  }
};

var DateStub = { now: function () { return now; } };

function advance(ms) {
  var target = now + ms;
  // Due timers fire in time order, so a one-second interval ticks once per second rather than once
  // per `advance` — which is what makes "the text keeps moving" assertable at all.
  for (;;) {
    var soonest = null;
    for (var id in timers) {
      if (!Object.prototype.hasOwnProperty.call(timers, id)) continue;
      if (timers[id].due > target) continue;
      if (soonest === null || timers[id].due < timers[soonest].due) soonest = id;
    }
    if (soonest === null) break;
    var timer = timers[soonest];
    now = timer.due;
    if (timer.every === null) delete timers[soonest];
    else timer.due = now + timer.every;
    timer.fn();
  }
  now = target;
}

new Function("document", "window", "Date", src)(documentStub, windowStub, DateStub);

var log = [];
function snap(label) {
  log.push({
    at: label,
    text: spinners.map(function (s) { return s.textContent; }),
    liveTimers: Object.keys(timers).length
  });
}

snap("initial");

fire("htmx:beforeRequest", { detail: { elt: buttons[0] } });
snap("request started");

advance(9000);
snap("nine seconds in");

advance(2000);
snap("eleven seconds in");

advance(9000);
snap("twenty seconds in");

fire("htmx:afterRequest", { detail: { elt: buttons[0] } });
snap("request finished");

// A second request gets the same treatment, counting from zero rather than resuming the first one's
// elapsed total — otherwise the second turn opens by claiming it has already been running a minute.
fire("htmx:beforeRequest", { detail: { elt: buttons[0] } });
advance(3000);
snap("second request, three seconds in");

// Coming back to a cached page has to stop the clock. A page that keeps counting after the work is
// done is the same lie in the other direction.
fire("pageshow", {});
snap("after pageshow");

process.stdout.write(JSON.stringify(log));
