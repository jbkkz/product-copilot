// A minimal DOM, enough to execute `web/static/js/app.js` for real and watch what it does to the
// page's submit buttons. Driven by `test_one_generation_at_a_time_is_the_pages_rule_not_the_forms`.
//
// Why this exists: the rule under test is "while any request is in flight, every submit button on the
// page is muted" (#50). That is browser behaviour, and the Python suite drives a `TestClient` which
// runs no JavaScript at all — so the alternative was asserting that a literal string appears in the
// asset, which pins the implementation's spelling rather than its effect and would pass just as well
// against an implementation that never disabled anything.
//
// The stub is deliberately tiny and deliberately strict: `querySelectorAll` throws on any selector it
// was not built for, so a rewrite that starts asking a different question fails loudly here instead of
// quietly observing nothing. `getElementById` returns null, so `start()`/`done()` take their early
// return and no timer is ever scheduled — this harness needs no clock.
"use strict";

var fs = require("fs");
var appJsPath = process.argv[2];
var src = fs.readFileSync(appJsPath, "utf8");

var SUBMIT_SELECTOR = 'button[type="submit"]';

function makeButton(name) {
  return {
    name: name,
    disabled: false,
    classList: { toggle: function () {} },
    matches: function (sel) { return sel.indexOf("submit") !== -1; },
    querySelector: function () { return null; }
  };
}

// Three buttons standing in for the page the issue describes: the primary brief form, one of the
// sibling generator forms under "More documents", and the answers form. All three post; only one of
// them is ever clicked in a given step.
var buttons = [makeButton("brief"), makeButton("generator-sibling"), makeButton("answers")];

var listeners = {};
function on(type, fn) { (listeners[type] = listeners[type] || []).push(fn); }
function fire(type, ev) {
  var fns = listeners[type] || [];
  for (var i = 0; i < fns.length; i++) fns[i](ev);
}

var bodyClasses = {};
var documentStub = {
  body: {
    classList: { toggle: function (c, v) { bodyClasses[c] = v; } },
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

// app.js is an IIFE reading bare `document` and `window`; supply both as parameters.
new Function("document", "window", src)(documentStub, windowStub);

var log = [];
function snap(label) {
  log.push({
    at: label,
    disabled: buttons.map(function (b) { return b.disabled; }),
    busy: bodyClasses.busy === true
  });
}
function req(b) { return { detail: { elt: b } }; }

snap("initial");

// Two generator buttons clicked in turn, which is the reported sequence.
fire("htmx:beforeRequest", req(buttons[0]));
snap("one in flight");
fire("htmx:beforeRequest", req(buttons[1]));
snap("two in flight");
fire("htmx:afterRequest", req(buttons[0]));
snap("first finished, second still running");
fire("htmx:afterRequest", req(buttons[1]));
snap("both finished");

// A swap replaces `#artifacts-region` mid-flight. The markup it brings in carries no disabled
// attribute, so the state has to be re-asserted over the new nodes.
fire("htmx:beforeRequest", req(buttons[0]));
buttons = [makeButton("swapped-in-a"), makeButton("swapped-in-b")];
snap("swapped in, before afterSwap");
fire("htmx:afterSwap", {});
snap("swapped in, after afterSwap");
fire("htmx:afterRequest", req(buttons[0]));
snap("after the swap, request finished");

// A bfcache restore has to clear the count, or a page returned to by Back is dead.
fire("htmx:beforeRequest", req(buttons[0]));
snap("in flight before pageshow");
fire("pageshow", {});
snap("after pageshow");

process.stdout.write(JSON.stringify(log));
