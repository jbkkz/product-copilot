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
The copy is verbatim from the upstream release; to update it, replace the file, bump the version
above, and re-run the web tests (`pytest tests/web -q`).
