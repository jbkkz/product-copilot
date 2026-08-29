# Third-party notices

Requivo itself is MIT-licensed (see `LICENSE`). Its Python dependencies are declared in
`pyproject.toml` and installed from PyPI, so they carry their own licenses and are not redistributed
here. This file covers the one thing that *is* copied into this repository and shipped inside the
wheel.

## htmx

- **Version:** 1.9.12
- **File:** `src/requivo/web/static/vendor/htmx.min.js`
- **Upstream:** https://htmx.org — https://github.com/bigskysoftware/htmx
- **License:** Zero-Clause BSD (0BSD)

```
Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee
is hereby granted.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS SOFTWARE
INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE
LIABLE FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING
FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS
ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
```

0BSD imposes no attribution requirement — this notice is here because a redistributed file should be
traceable to its source and version, not because the license demands it.

**Why it is vendored rather than fetched:** Requivo Web sets a strict Content-Security-Policy that
allows same-origin assets only, and it is meant to work offline. A CDN script tag would violate both.

**How it is updated, and who does it.** The maintainer, by hand, because nothing else can: a
minified `.js` file appears in no dependency manifest, so `.github/dependabot.yml` cannot watch it
and neither can any advisory scanner (#297). The version above is therefore the only record that
this file has a version at all — which is why it is stated here rather than left to the file's own
banner. The procedure:

1. Download the release from <https://github.com/bigskysoftware/htmx/releases> (`htmx.min.js`).
2. Replace `src/requivo/web/static/vendor/htmx.min.js` verbatim — no local edits, ever, or the
   version above stops describing what is shipped.
3. Bump the **Version** line above.
4. Re-run the web tests: `pytest tests/web -q`.

No Node toolchain is added for one file, and none should be. 1.9.12 is on the 1.x maintenance line,
superseded by 2.x; moving is a deliberate decision rather than a routine bump, since 2.x changes
default behaviours the templates rely on.
