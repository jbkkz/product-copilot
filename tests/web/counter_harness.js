// A minimal DOM, enough to execute `web/static/js/app.js` for real and watch what the character
// counter does to a field a reader is typing into. Driven by
// `test_the_character_counter_counts_and_warns_without_ever_touching_the_text`.
//
// Why this exists: #239 asks for an affordance that **counts and warns and never clips**, which is
// invariant 3 at the one place a real user meets it. The reflex implementation is `maxlength`, and
// that is the bug (#8): a browser drops everything past the ceiling with no event, no message and no
// visual difference, so a 25,000-character client email arrives at exactly the ceiling and passes
// every check the server has. A `TestClient` runs no JavaScript, so the alternative was asserting
// that a literal string appears in the asset — which pins the spelling rather than the effect and
// passes just as well against code that trims the field on every keystroke.
//
// The two observations that make this test about the rule rather than about the wording are the ones
// a text assertion cannot make: `value` is a real accessor, so any write by the page is recorded and
// the "never trims" claim is observed rather than argued; and `setAttribute` is recorded, so a
// counter that quietly grows a clipping attribute of its own is caught here too.
//
// Sibling of `busy_harness.js`, `elapsed_harness.js` and `error_swap_harness.js`, deliberately the
// same shape: tiny, strict about the selectors it models, and reporting a timeline rather than an
// assertion, so the Python side owns what the observations mean.
"use strict";

var fs = require("fs");
var appJsPath = process.argv[2];
var src = fs.readFileSync(appJsPath, "utf8");

var SUBMIT_SELECTOR = 'button[type="submit"]';
var SPINNER_SELECTOR = ".spinner";
var FIELD_SELECTOR = "textarea[data-limit]";

var LIMIT = 20000;

function makeNode(tag) {
  return {
    tagName: tag,
    className: "",
    textContent: "",
    attributes: {},
    setAttribute: function (name, value) { this.attributes[name] = value; },
    getAttribute: function (name) {
      return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
    }
  };
}

// A textarea whose `value` is a real accessor: the harness writes it to simulate typing, and any
// write the page makes is recorded separately. That is what turns "it must never trim" from a claim
// into an observation — a page that clipped the text would appear in `writes` even if the visible
// count still looked right.
function makeField(name, limit, initial) {
  var field = makeNode("TEXTAREA");
  var text = initial || "";
  var writes = [];
  var siblings = [];
  field.name = name;
  field.attributes["data-limit"] = String(limit);
  field.parentNode = {
    insertBefore: function (node) { siblings.push(node); }
  };
  field.harness = {
    writes: writes,
    siblings: siblings,
    type: function (n) { text = new Array(n + 1).join("x"); }
  };
  Object.defineProperty(field, "value", {
    get: function () { return text; },
    set: function (v) { writes.push(v.length); text = v; }
  });
  return field;
}

// `request` is on the page from the start. `answers` arrives later, already carrying text: that is
// the refusal re-render (#30), where the server hands the form back with what the reader submitted
// still in it — so the count has to be right before a single key is pressed.
var request = makeField("request_text", LIMIT, "");
var fields = [request];

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
  createElement: makeNode,
  querySelectorAll: function (sel) {
    if (sel === SUBMIT_SELECTOR) return [];
    if (sel === SPINNER_SELECTOR) return [];
    if (sel === FIELD_SELECTOR) return fields;
    throw new Error("the harness models " + SUBMIT_SELECTOR + ", " + SPINNER_SELECTOR + " and "
      + FIELD_SELECTOR + ", got: " + sel);
  },
  addEventListener: on
};

var windowStub = {
  addEventListener: on,
  setInterval: function () { return 0; },
  clearInterval: function () {},
  setTimeout: function () { return 0; }
};

new Function("document", "window", src)(documentStub, windowStub);

var log = [];
function snap(label, field) {
  var counter = field.harness.siblings.length ? field.harness.siblings[0] : null;
  log.push({
    at: label,
    field: field.name,
    // What the reader sees. An empty string is the deliberate silence below the threshold: the
    // counter node exists but says nothing, so its appearance is what carries the signal.
    text: counter ? counter.textContent : null,
    className: counter ? counter.className : null,
    live: counter ? counter.getAttribute("aria-live") : null,
    // What the page did to the field. Both must stay empty for the whole timeline.
    writes: field.harness.writes.slice(),
    fieldAttributes: Object.keys(field.attributes).sort(),
    length: field.value.length
  });
}

function typeInto(field, n) {
  field.harness.type(n);
  fire("input", { target: field });
}

snap("initial", request);

typeInto(request, 15999);
snap("just under the threshold", request);

typeInto(request, 16000);
snap("at the threshold", request);

typeInto(request, LIMIT);
snap("exactly at the ceiling", request);

typeInto(request, LIMIT + 1);
snap("one over the ceiling", request);

typeInto(request, 100);
snap("back down again", request);

// The swap path: a field the page did not have at load, arriving with the reader's refused text
// already in it. Nothing is typed into it — only `htmx:afterSwap` fires.
var answers = makeField("answers", LIMIT, new Array(19001).join("x"));
fields.push(answers);
fire("htmx:afterSwap", {});
snap("swapped in, already full", answers);

// ── the ceiling the page cannot read ──────────────────────────────────────────
//
// `updateCounter` refuses to count against a number it had to invent, and refusing is the right
// answer: a guessed ceiling would warn about a submission the server accepts, or stay quiet about
// one it refuses. Both spellings of "cannot read" are driven here, because the guard is
// `!(limit > 0)` and only one of them produces `NaN`.
var unreadable = makeField("unreadable", 0, new Array(19001).join("x"));
unreadable.attributes["data-limit"] = "not-a-number";
fields.push(unreadable);

var zeroed = makeField("zeroed", 0, new Array(19001).join("x"));
zeroed.attributes["data-limit"] = "0";
fields.push(zeroed);

fire("htmx:afterSwap", {});
snap("ceiling unparseable", unreadable);
snap("ceiling is zero", zeroed);

// …and a textarea that declares no ceiling at all. The sweep selector would never return it in a
// browser, so the only way the page meets one is the delegated `input` listener, which sees every
// input event on the document — including one from a field that is none of its business.
var unlimited = makeField("unlimited", 0, "");
delete unlimited.attributes["data-limit"];
unlimited.harness.type(500);
fire("input", { target: unlimited });
snap("no ceiling declared", unlimited);

process.stdout.write(JSON.stringify(log));
