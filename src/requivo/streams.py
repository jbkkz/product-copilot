"""The process's standard streams — the one place their encoding is decided (#29).

`paths.py` owns the environment; this module owns stdout and stderr. Core touches neither (invariant
7), `render/` turns data into strings, and the interfaces print them — so there is exactly one place
where the question *what happens when the console cannot represent this character* has to be
answered, and this is it.

The bug this exists to remove
-----------------------------
Text written to stdout is encoded with the **console's** codepage, not the source file's. On Windows
that is typically cp1252, and on any platform a redirected or piped stream falls back to the locale's
codec. A check mark, a box-drawing rule, an arrow or an em dash then raises `UnicodeEncodeError` and
kills the process **at the `print`** — after the work that print was reporting has already landed.

The ordering is the whole defect. `requivo doctor` dies on the check mark of its very first line,
having already computed the diagnosis it exists to report, and the exit code then describes the
crash rather than the finding. Worse, `requivo brief <session> > brief.txt`: the paid provider call
has completed, the revision has been applied and the artifact written, and only then does the
renderer die — so the operator reads a crash, re-runs, and pays for a second call while applying a
second revision on top of the first.

Why a chokepoint and not a glyph table
--------------------------------------
Choosing ASCII-safe characters fixes today's strings and not tomorrow's. `doctor` alone prints three
different non-ASCII glyphs, `render/terminal.py` prints nine more, **every** error message in the
package carries an em dash, and `artifact show` prints provider-written prose straight to stdout —
one curly quote from the model reopens the bug with none of our characters involved. Forty string
edits would be permanently re-openable by the next em dash anybody types.

The two decisions, and what each one costs
------------------------------------------
**`errors="backslashreplace"`, always.** This is the part that is not negotiable: a renderer must
not be able to kill a process after the mutation it was reporting has landed. `backslashreplace` is
chosen over `replace` deliberately — `replace` prints a question mark where a character was, and a
reader cannot tell a substituted character from a character that was never there. An escape is ugly
and honest; a hole is neither.

**`encoding="utf-8"` unless the operator named a codec.** Where stdout is a real console this is a
no-op (Windows consoles have been Unicode-capable since 3.6). Where it is redirected it is the whole
point: `requivo brief <session> > brief.md` should produce a UTF-8 document somebody can read and
share, not a cp1252 file with escapes standing in for every dash.

`PYTHONIOENCODING` is honoured rather than overridden. An operator who names a codec has made a
decision about their pipeline, and second-guessing it would be this module deciding something that
is not its to decide. What it still guarantees for them is the error handler: their codec, and it
cannot crash. That is also what makes the guarantee testable — `PYTHONIOENCODING=ascii` reaches a
real encoder in a subprocess, on every platform, which is how `tests/test_encoding.py` exercises
this on the Linux legs rather than waiting for a Windows one.

The third state
---------------
A stream can refuse to be reconfigured: a `StringIO` a test substituted, a pipe already detached, an
sdk-wrapped stream that is not a `TextIOWrapper`. `configure_streams` reports that per stream rather
than raising (it is called before argument parsing, where a traceback would be the least useful
thing a user could get) and rather than staying silent (a stream that could not be configured is
exactly the one that can still crash later). `doctor` prints the report, so *could not configure*
is a line somebody can read instead of an absence.
"""

from __future__ import annotations

import os
import sys

# What every stream gets, whatever its codec. See the module docstring for why it is
# `backslashreplace` and not `replace`.
ERRORS = "backslashreplace"

# What a stream gets when the operator has not named a codec.
PREFERRED_ENCODING = "utf-8"


def _target_encoding(stream) -> str | None:
    """The codec to move `stream` to, or None to leave whatever it already has.

    None is returned when `PYTHONIOENCODING` is set: the operator named a codec for this pipeline and
    it is not this module's decision to overrule. They still get the error handler.
    """
    if os.environ.get("PYTHONIOENCODING"):
        return None
    current = (getattr(stream, "encoding", "") or "").lower().replace("_", "-")
    if current in ("utf-8", "utf8"):
        return None  # already there; reconfiguring would be a no-op with a failure mode
    return PREFERRED_ENCODING


def configure_stream(stream, name: str) -> dict:
    """Make one stream unable to kill the process on a character it cannot encode.

    Returns a report rather than raising or staying quiet — the three states are `configured`,
    `unchanged` (there was nothing to do) and `could-not` (with the reason). A stream this could not
    reach is the one that can still crash, so it must be nameable downstream.
    """
    report = {
        "stream": name,
        "state": "could-not",
        "encoding_before": getattr(stream, "encoding", None),
        "encoding_after": None,
        "errors": getattr(stream, "errors", None),
        "reason": None,
    }
    if stream is None:
        report["reason"] = "the stream is None (pythonw, or a detached process)"
        return report
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        report["reason"] = (
            f"{type(stream).__name__} has no reconfigure(); it is not a TextIOWrapper, so its "
            f"codec is whatever substituted it"
        )
        return report
    encoding = _target_encoding(stream)
    try:
        reconfigure(encoding=encoding, errors=ERRORS)
    except (ValueError, OSError, LookupError) as e:
        # ValueError: the stream is closed or detached. OSError: an unseekable stream on some
        # platforms. LookupError: a codec name the interpreter does not know.
        report["reason"] = f"{type(e).__name__}: {e}"
        return report
    report["state"] = "configured" if encoding else "unchanged"
    report["encoding_after"] = getattr(stream, "encoding", None)
    report["errors"] = getattr(stream, "errors", None)
    if not encoding:
        report["reason"] = (
            "PYTHONIOENCODING names the codec" if os.environ.get("PYTHONIOENCODING")
            else "already UTF-8"
        )
    return report


def configure_streams() -> list:
    """Configure stdout and stderr. Called once, first thing in `cli.app()`.

    Deliberately not called at import time: importing `requivo` must not reconfigure the streams of
    a program that merely imported it. The Web surface never prints, and the library API has no
    business touching a caller's stdout — so the CLI entry point is the only caller.
    """
    return [configure_stream(sys.stdout, "stdout"), configure_stream(sys.stderr, "stderr")]


# Error handlers that substitute something a reader can *see*. A stream on one of these can neither
# crash nor lose a character silently.
_VISIBLE_ERROR_HANDLERS = frozenset({"backslashreplace", "xmlcharrefreplace", "namereplace"})

# Handlers that cannot crash but do lose information without saying so: `replace` prints a question
# mark, `ignore` prints nothing at all. Reporting these as `safe` would have `doctor` endorse exactly
# the quiet hole this module's docstring argues against -- so they get their own verdict. Requivo
# never selects one; a stream arrives on one because the operator asked for it via PYTHONIOENCODING.
_LOSSY_ERROR_HANDLERS = frozenset({"replace", "ignore"})


def describe_stream(stream, name: str) -> dict:
    """What `stream` is set to *now*, with no side effect. What `doctor` reports.

    Four states, and the last two are the reason this is a separate function from `configure_stream`:

      - `safe` — a character it cannot encode is substituted with something a reader can see;
      - `lossy` — it cannot crash, but it drops or blanks the character silently. Not `safe`: a
        reader cannot tell a substituted character from one that was never there, which is the
        failure this module's docstring rejects, and `doctor` reporting it as clean would be this
        project endorsing it;
      - `will_crash` — a strict handler, so a character it cannot encode kills the process at the
        print, after the work that print was reporting has landed (the #29 shape);
      - `unknown` — the stream does not expose a codec at all, so this cannot answer and must not
        pretend to.
    """
    encoding = getattr(stream, "encoding", None)
    errors = getattr(stream, "errors", None)
    if stream is None or encoding is None:
        return {"stream": name, "state": "unknown", "encoding": None, "errors": errors,
                "detail": f"{type(stream).__name__} does not expose an encoding; this check cannot look"}
    handler = errors or "strict"
    if handler in _VISIBLE_ERROR_HANDLERS:
        return {"stream": name, "state": "safe", "encoding": encoding, "errors": errors, "detail": None}
    if handler in _LOSSY_ERROR_HANDLERS:
        return {
            "stream": name, "state": "lossy", "encoding": encoding, "errors": errors,
            "detail": f"{encoding} with errors={errors!r}: a character it cannot encode is dropped or "
                      f"blanked with no mark, so a reader cannot tell it from one that was never there",
        }
    return {
        "stream": name, "state": "will_crash", "encoding": encoding, "errors": errors,
        "detail": f"{encoding} with errors={errors!r}: a character it cannot encode raises "
                  f"UnicodeEncodeError and kills the process at the print",
    }


def describe_streams() -> list:
    return [describe_stream(sys.stdout, "stdout"), describe_stream(sys.stderr, "stderr")]


def safe_write(stream, text: str) -> None:
    """Write `text` to `stream`, never raising on a character the stream cannot encode.

    The belt to `configure_streams`' braces, for the narrow case it cannot cover: a stream that
    reported `could-not` above is still a stream this process is about to print an error message to,
    and an error report that dies on its own em dash is the failure this whole module is about.
    """
    try:
        stream.write(text)
    except UnicodeEncodeError:
        encoding = (getattr(stream, "encoding", None) or "ascii")
        try:
            stream.write(text.encode(encoding, ERRORS).decode(encoding, "replace"))
        except (ValueError, OSError, UnicodeError):
            # The stream can close between the first attempt and the retry. Guarded for the same
            # reason the first write is: this function is usually reporting an error, and an error
            # reporter that raises replaces the message with a traceback about the message.
            return
    except (ValueError, OSError):
        return  # a closed or broken stream: there is nowhere left to report to
    try:
        stream.flush()
    except (ValueError, OSError):
        pass
