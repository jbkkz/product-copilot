// Requivo Web — minimal progressive enhancement. The app works without this file; HTMX (vendored
// locally) drives the partial updates. Here we add a visible loading signal so it's always clear that
// something is happening — both for HTMX swaps (answers, generation) and for full-page form navigations
// (creating a session, running discovery, which otherwise block with no feedback).
(function () {
  "use strict";

  var bar = document.getElementById("progress");
  var timer = null;

  // Timers go through `window` rather than the bare globals so a test harness can supply its own
  // clock. The elapsed signal below is behaviour *over time*, and a test that waits eleven real
  // seconds for it is either slow or a race; with an injected clock the ten-second boundary is
  // asserted exactly. Identical in a browser, where these are the same functions.
  function every(fn, ms) { return window.setInterval(fn, ms); }
  function stop(id) { window.clearInterval(id); }

  function start() {
    if (!bar) return;
    bar.classList.add("on");
    var w = 12;
    bar.style.width = w + "%";
    stop(timer);
    // ease toward 90% while we wait; the request completing (or the page navigating) finishes it.
    timer = every(function () { w += (90 - w) * 0.12; bar.style.width = w + "%"; }, 300);
  }

  function done() {
    if (!bar) return;
    stop(timer);
    bar.style.width = "100%";
    window.setTimeout(function () { bar.classList.remove("on"); bar.style.width = "0%"; }, 350);
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

  // **A wait that outlives its own copy has to keep speaking** (#236). The provider call is
  // synchronous and the page blocks on it, for what invariants 2 and 12 describe as "seconds to
  // minutes". Until this, the only status text was a fixed label — so past the point where a
  // first-time reader expected an answer, the page looked exactly like one that had hung, and the
  // natural next move on a blocked create is to reload or re-paste: a second session and a second
  // paid call.
  //
  // Nothing happens for the first ten seconds, deliberately. A label that churns from the start is
  // decoration on a fast call and carries no information on a slow one; the *change* is the signal,
  // so it has to mean "this is taking longer than the copy promised" and nothing else. The original
  // label is restored when the work ends, or a finished page would still read "still working".
  // Pinned by `test_a_long_call_says_so_after_ten_seconds_rather_than_looking_stuck`, which drives
  // this file against an injected clock.
  var ELAPSED_AFTER_MS = 10000;
  var LABEL_ATTR = "data-label";
  var elapsedTimer = null;
  var elapsedFrom = 0;

  function statusNodes() {
    // Queried fresh, for the same reason `applyBusy` does it: an htmx swap replaces these nodes
    // mid-flight, and a saved list would keep writing into elements the document no longer holds.
    return document.querySelectorAll(".spinner");
  }

  function baseLabel(node) {
    var stored = node.getAttribute(LABEL_ATTR);
    if (stored === null || stored === undefined) {
      stored = node.textContent;
      node.setAttribute(LABEL_ATTR, stored);
    }
    return stored;
  }

  function tickElapsed() {
    var ms = Date.now() - elapsedFrom;
    if (ms < ELAPSED_AFTER_MS) return;
    var seconds = Math.floor(ms / 1000);
    var nodes = statusNodes();
    for (var i = 0; i < nodes.length; i++) {
      nodes[i].textContent = baseLabel(nodes[i]) + " still working (" + seconds + "s)";
    }
  }

  function startElapsed() {
    elapsedFrom = Date.now();
    stop(elapsedTimer);
    elapsedTimer = every(tickElapsed, 1000);
  }

  function stopElapsed() {
    stop(elapsedTimer);
    elapsedTimer = null;
    var nodes = statusNodes();
    for (var i = 0; i < nodes.length; i++) {
      var stored = nodes[i].getAttribute(LABEL_ATTR);
      if (stored !== null && stored !== undefined) nodes[i].textContent = stored;
    }
  }

  function applyBusy() {
    var busy = inFlight > 0;
    document.body.classList.toggle("busy", busy);
    // Queried fresh every time: an HTMX swap replaces the buttons mid-flight, so a saved node list
    // would re-enable elements the document no longer holds and leave the new ones disabled.
    var buttons = document.querySelectorAll('button[type="submit"]');
    for (var i = 0; i < buttons.length; i++) buttons[i].disabled = busy;
  }

  // Known limit, inert today and written down so it stays that way. HTMX exits early when a listener
  // cancels `htmx:beforeRequest`, and no `afterRequest` follows — but our listener has already
  // incremented, so the page would stay muted until the next `pageshow`. Nothing here cancels that
  // event: there is no `hx-confirm` and no `preventDefault` on it anywhere in `web/`. Adding one is
  // what would reintroduce this, and reading `defaultPrevented` from our own listener cannot fix it
  // because listener order decides who runs first. The clamp below keeps the count from going
  // negative, which is the other half of the same bookkeeping.
  function setBusy(on) {
    var was = inFlight;
    inFlight = Math.max(0, inFlight + (on ? 1 : -1));
    // The elapsed clock is scoped to the whole page's busy period, not to one request, for the same
    // reason the button muting is: two generations can be in flight at once, and restarting the
    // count on the second would reset a reader's sense of how long they have been waiting, while
    // stopping it on the first finishing would go quiet while work is still running.
    if (was === 0 && inFlight > 0) startElapsed();
    else if (was > 0 && inFlight === 0) stopElapsed();
    applyBusy();
  }

  // **A notice outlives the thing it was about unless something clears it** (#320). `#flash` is
  // written by every retargeted error and by nothing else, so after an artifact generation 409 the
  // reader can go on to submit the answers form successfully, watch `#session-body` swap, and still
  // be looking at the old error — "still broken" and "already fixed" rendering identically, which is
  // the failure this app is careful about everywhere else. Only a full page navigation cleared it,
  // and the htmx paths never navigate. Clearing on `beforeRequest` is the narrow fix: a new request
  // supersedes the last one's complaint, and a fresh error re-fills the region a moment later.
  function clearFlash() {
    var flash = document.getElementById("flash");
    if (flash) flash.innerHTML = "";
  }

  // HTMX partial requests (answers, artifact generation).
  document.body.addEventListener("htmx:beforeRequest", function (e) {
    clearFlash(); start(); markLoading(e.detail.elt, true); setBusy(true);
  });
  document.body.addEventListener("htmx:afterRequest", function (e) {
    done(); markLoading(e.detail.elt, false); setBusy(false);
  });
  // A swap brings in markup that carries no disabled attribute, so re-assert the state over it: with
  // one request in flight that is a no-op, and with two it is the difference between the first one
  // finishing and quietly handing the reader live buttons while the second is still running.
  document.body.addEventListener("htmx:afterSwap", applyBusy);

  // **Error responses have to reach the eye** (#203). The vendored htmx (1.9.12) swaps only
  // 200-399, so every 4xx/5xx fragment this app builds was dropped on the floor: the progress bar
  // completed, the buttons came back, the page did not change and nothing was said. A revision
  // conflict from a second tab, the 413 that #30 built to preserve your typed answers, and a 502
  // after a minutes-long *paid* call all looked identical to success-with-no-visible-effect — and on
  // the paid one the natural next move is to click again and pay again. The whole server-side error
  // architecture was unreachable, and the Python suite could not see it because `TestClient` runs no
  // JavaScript.
  //
  // Opting in is safe because the server always returns something renderable for these: either a
  // full region (the 413 answers path) or the small `errors/_error.html` fragment, which arrives with
  // `HX-Retarget: #flash` so it lands in the always-present flash region instead of replacing the
  // region that holds the reader's work. `isError` is cleared so an expected, handled response stops
  // logging as an uncaught one.
  //
  // Pinned by `test_error_responses_are_swapped_into_the_page_rather_than_dropped`, which drives this
  // file for real — asserting a literal string appears in the asset would pass against an
  // implementation that swaps nothing.
  document.body.addEventListener("htmx:beforeSwap", function (e) {
    var status = e.detail.xhr ? e.detail.xhr.status : 0;
    if (status >= 400) {
      e.detail.shouldSwap = true;
      e.detail.isError = false;
    }
  });

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

  // ── counting, never clipping (#239) ────────────────────────────────────────
  //
  // The server *refuses* an over-long request rather than trimming it (invariant 3): half a request
  // folded into the model reads exactly like a whole one, so the reader would never learn which half
  // the engine saw. That is why no field in this app carries `maxlength` — a browser drops
  // everything past the ceiling with no event, no message and no visual difference, so an over-long
  // paste arrives at exactly the ceiling and sails through the very check written to stop it (#8),
  // and `test_no_template_carries_a_clipping_attribute` keeps it that way.
  //
  // What was missing is the other half of that decision. `docs/web.md` asked for it in as many
  // words — an affordance is welcome, but it has to count and warn and must never trim — and until
  // now the only feedback for a 26,000-character client email was a refusal after the submit.
  //
  // So this counts and warns and does nothing else: it never assigns to the field, never puts an
  // attribute on it, and never blocks a submit. An over-long submission still goes to the server and
  // still comes back refused, with what was typed preserved in the form (#30). With JavaScript off
  // nothing here runs and the page behaves exactly as before — no counter, and still no clipping,
  // which is the only one of the two that would be a bug.
  //
  // Below 80% of the ceiling it says nothing at all. A counter that is always on is decoration and
  // carries no information at the moment it matters; its *appearance* is the signal.
  //
  // Pinned by `test_the_character_counter_counts_and_warns_without_ever_touching_the_text`, which
  // drives this file against a DOM whose field records every write it receives — so "never trims" is
  // observed rather than argued.
  var COUNTER_SHOWS_AT = 0.8;
  var LIMIT_ATTR = "data-limit";
  var FIELD_SELECTOR = "textarea[" + LIMIT_ATTR + "]";
  var OVER_LIMIT_NOTE = " — over the limit; this will be refused when you submit.";

  // Grouped by hand rather than through `toLocaleString`, whose separator comes from the *runtime's*
  // locale: the same code says "18,400" on one machine and "18 400" or "18.400" on another, so the
  // page and its test would only agree by accident of where they ran.
  function grouped(n) {
    var digits = String(n), out = "", i;
    for (i = 0; i < digits.length; i++) {
      if (i > 0 && (digits.length - i) % 3 === 0) out += ",";
      out += digits.charAt(i);
    }
    return out;
  }

  // Created next to the field rather than rendered by the template, so the no-JS page carries no
  // empty region that never fills. `aria-live=polite` because the count is a state change a reader
  // who is not looking at it still needs; `polite` and not `assertive` — it is a heads-up, not an
  // interruption. The node is kept on the field itself, so a swap that replaces the textarea gets a
  // fresh counter rather than one writing into a node the document no longer holds.
  function counterFor(field) {
    if (field.requivoCounter) return field.requivoCounter;
    var node = document.createElement("p");
    node.className = "counter";
    node.setAttribute("aria-live", "polite");
    if (field.parentNode) field.parentNode.insertBefore(node, field.nextSibling);
    field.requivoCounter = node;
    return node;
  }

  function updateCounter(field) {
    var limit = parseInt(field.getAttribute(LIMIT_ATTR), 10);
    // A field whose ceiling we cannot read gets no counter at all, rather than one counting against
    // a number this file invented. A guessed ceiling is worse than none: it would warn about a
    // submission the server accepts, or stay quiet about one it refuses.
    if (!(limit > 0)) return;
    var used = field.value ? field.value.length : 0;
    var node = counterFor(field);
    if (used < limit * COUNTER_SHOWS_AT) {
      // Read off the field every time and never latched, so deleting text takes the counter away
      // again.
      node.className = "counter";
      node.textContent = "";
      return;
    }
    // The ceiling is the maximum *permitted* length, not the first refused one — the server accepts
    // exactly `MAX_INPUT_CHARS` — so `>` and not `>=`: styling the legal boundary as an error would
    // tell the reader a submission that will be accepted is about to be refused.
    var over = used > limit;
    node.className = over ? "counter danger" : "counter";
    node.textContent = grouped(used) + " / " + grouped(limit) + " characters"
      + (over ? OVER_LIMIT_NOTE : "");
  }

  function refreshCounters() {
    var fields = document.querySelectorAll(FIELD_SELECTOR);
    for (var i = 0; i < fields.length; i++) updateCounter(fields[i]);
  }

  // Delegated, so a textarea an htmx swap brought in is covered without re-binding anything.
  document.addEventListener("input", function (e) {
    var field = e.target;
    if (field && field.getAttribute && field.getAttribute(LIMIT_ATTR) !== null) updateCounter(field);
  });
  // …and swept after a swap as well, because the count has to be right *before* a key is pressed:
  // the answers refusal re-renders the region with what the reader submitted still in the field
  // (#30), which is precisely the case where they are already over the ceiling.
  document.body.addEventListener("htmx:afterSwap", refreshCounters);
  // The same reasoning for the first paint: the request form comes back from a refusal carrying the
  // text that was refused.
  refreshCounters();

  // Reset the bar if the user navigates back to a cached page.
  window.addEventListener("pageshow", function () {
    if (bar) { bar.classList.remove("on"); bar.style.width = "0%"; }
    inFlight = 0;
    // …and the clock with it. A restored page that keeps counting against a request that finished
    // before the reader navigated away is the same lie as a page that says nothing, inverted.
    stopElapsed();
    applyBusy();
  });
})();
