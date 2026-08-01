"""The deterministic CLI surface — every command here runs with no LLM and no API key.

These verbs (`doctor`, `session …`, `model …`, `artifact …`) are the offline half of Requivo: they
create and inspect sessions, validate and apply proposed models, and record artifacts, all through
the same `SessionService`/`ArtifactService` the provider path uses. Claude Code drives *these* — it
reasons with its own Claude, writes a proposal file, and calls `model validate`/`model apply` — so no
`ANTHROPIC_API_KEY` is ever required in that mode.

`register(sub)` attaches the parsers to the main `requivo` argparse tree; each handler takes
`(args, client)` to match the CLI's uniform dispatch (the deterministic handlers ignore `client`).
Handlers raise structured `RequivoError`s; `cli.app()` turns them into a clean message or a JSON error
envelope (`--json`).
"""

from __future__ import annotations

import json
import os
import platform
import zipfile
from pathlib import Path

from requivo.core import persistence as store
from requivo.core.context import available_cards
from requivo.core.errors import InvalidModelError, SessionNotFoundError
from requivo.core.validation import validate_proposal
from requivo.paths import ASSETS, session_root, workspace_root
from requivo.services.artifacts import ARTIFACT_FILENAMES, ArtifactService
from requivo.services.sessions import SessionService


def _print_json(obj) -> None:
    print(json.dumps(obj, indent=2))


def _read_source(arg: str) -> str:
    """A request/answers argument that may be an inline string or a path to a file."""
    try:
        is_file = bool(arg.strip()) and Path(arg).is_file()
    except OSError:
        is_file = False
    return Path(arg).read_text() if is_file else arg


# ── doctor ──────────────────────────────────────────────────────────────────────


def doctor_report() -> dict:
    """A self-diagnosis of the install: Python, Requivo, assets, schema, provider availability, and
    the workspace. Absence of the Anthropic SDK / API key is reported as informational, NOT an error —
    Claude Code mode needs neither."""
    from requivo import __version__

    # Assets + schema.
    schema_ok, slot_count, schema_err = True, 0, None
    try:
        from requivo.core.contracts import schema_slot_ids
        allowed, _ = schema_slot_ids()
        slot_count = len(allowed)
    except Exception as e:  # noqa: BLE001 - doctor reports any failure rather than raising
        schema_ok, schema_err = False, str(e)

    cards = []
    try:
        cards = available_cards()
    except Exception as e:  # noqa: BLE001
        schema_err = schema_err or str(e)

    # Provider (optional).
    provider_installed, provider_version = False, None
    try:
        import anthropic
        provider_installed = True
        provider_version = getattr(anthropic, "__version__", "unknown")
    except ImportError:
        pass

    return {
        "requivo_version": __version__,
        "python_version": platform.python_version(),
        "assets": {"root": str(ASSETS), "present": ASSETS.exists()},
        "schema": {"ok": schema_ok, "slots": slot_count, "error": schema_err},
        "context_cards": cards,
        "provider_anthropic": {
            "installed": provider_installed,
            "version": provider_version,
            "api_key_present": bool(os.getenv("ANTHROPIC_API_KEY")),
        },
        "workspace": {"root": str(workspace_root()), "sessions": str(session_root())},
    }


def _cmd_schema(a, client) -> None:
    """Print the slot schema (and optionally the human framework spec) so a reasoning caller — Claude
    Code, above all — has the exact slot vocabulary + driver rule to produce a valid proposal offline."""
    from requivo.paths import FRAMEWORK
    print((FRAMEWORK / "model_schema.json").read_text())
    if a.framework:
        print("\n\n<!-- framework/elicitation.md (human spec) -->\n")
        print((FRAMEWORK / "elicitation.md").read_text())


def _cmd_context(a, client) -> None:
    """List or print the context cards — the product knowledge that grounds impact estimation. A
    reasoning caller reads this to weigh information value; pure asset I/O, no LLM."""
    from requivo.core.context import load_context
    if a.list:
        for c in available_cards():
            print(c)
        return
    cards = _resolve_cards(a.cards) if a.cards else None
    print(load_context(cards))


def _cmd_doctor(a, client) -> None:
    r = doctor_report()
    if a.json:
        _print_json(r)
        return
    ok = "✅"
    warn = "🟡"
    print("Requivo doctor")
    print(f"  {ok} requivo         {r['requivo_version']}")
    print(f"  {ok} python          {r['python_version']}")
    print(f"  {ok if r['assets']['present'] else '❌'} assets          {r['assets']['root']}")
    s = r["schema"]
    print(f"  {ok if s['ok'] else '❌'} schema          {s['slots']} slots"
          + (f"  (error: {s['error']})" if not s["ok"] else ""))
    print(f"  {ok} context cards   {len(r['context_cards'])} available")
    p = r["provider_anthropic"]
    prov = f"installed (v{p['version']})" if p["installed"] else "not installed"
    key = "API key set" if p["api_key_present"] else "no API key"
    print(f"  {ok if p['installed'] else warn} anthropic       {prov} · {key}")
    if not p["installed"]:
        print("     └─ optional: `pip install 'requivo[anthropic]'` for API-powered discovery.")
        print("        Not needed for Claude Code mode.")
    print(f"  {ok} workspace       {r['workspace']['root']}")
    print(f"     sessions        {r['workspace']['sessions']}")


# ── session ──────────────────────────────────────────────────────────────────────


def _resolve_cards(spec: str | None) -> list[str] | None:
    """A comma-separated --context spec → validated card stems (None == all cards). Unknown cards are
    a hard error here (deterministic path): a typo shouldn't silently widen the context."""
    if not spec:
        return None
    avail = {c.lower(): c for c in available_cards()}
    picked, unknown = [], []
    for tok in spec.split(","):
        key = tok.strip().lower()
        if not key:
            continue
        (picked if key in avail else unknown).append(avail.get(key, tok.strip()))
    if unknown:
        raise InvalidModelError(
            f"unknown context card(s): {', '.join(unknown)}. Available: {', '.join(available_cards())}",
            details={"unknown": unknown},
        )
    return picked or None


def _cmd_session_init(a, client) -> None:
    request = _read_source(a.request)
    if not request.strip():
        raise InvalidModelError("session init needs a request (a sentence or a file path)")
    cards = _resolve_cards(a.context)
    meta = SessionService().create_session(
        request, context_cards=cards, slug=a.slug, provider=a.provider)
    if a.json:
        _print_json({"slug": meta.slug, "session_id": meta.session_id,
                     "path": str(store.canonical_dir(meta.slug)), "context_cards": meta.context_cards})
        return
    print(f"Created session '{meta.slug}' → {store.canonical_dir(meta.slug)}")
    print("  No model yet. Produce a proposal and run:")
    print(f"    requivo model apply {meta.slug} proposal.json")


def _cmd_session_list(a, client) -> None:
    sessions = SessionService().list_sessions()
    if a.json:
        _print_json([{"slug": m.slug, "revision": m.current_revision, "provider": m.provider,
                      "updated_at": m.updated_at} for m in sessions])
        return
    if not sessions:
        print(f"No sessions under {session_root()}.")
        return
    print(f"Sessions under {session_root()}:")
    for m in sessions:
        print(f"  {m.slug:<40} rev {m.current_revision}  ({m.provider or '—'}, {m.updated_at})")


def _cmd_session_show(a, client) -> None:
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    if not store.session_exists(slug):
        raise SessionNotFoundError(f"no canonical session '{slug}'", details={"slug": slug})
    meta = store.read_meta(slug)
    if a.json:
        _print_json(meta.model_dump())
        return
    print(f"Session '{meta.slug}'  (id {meta.session_id[:12]}…)")
    print(f"  created  {meta.created_at}")
    print(f"  updated  {meta.updated_at}")
    print(f"  revision {meta.current_revision}")
    print(f"  provider {meta.provider or '—'}   model {meta.model_name or '—'}")
    print(f"  context  {', '.join(meta.context_cards) if meta.context_cards else 'all cards'}")
    if meta.artifact_status:
        print("  artifacts:")
        for t, st in meta.artifact_status.items():
            stale = st.stale or st.revision != meta.current_revision
            print(f"    {t:<12} {st.filename:<26} rev {st.revision}  {'STALE' if stale else 'fresh'}")


def _cmd_session_migrate(a, client) -> None:
    """The explicit, opt-in bulk migration of every legacy out/<slug>/ session into the canonical
    store (the automatic path only migrates a session on its own first mutation)."""
    from requivo.paths import output_root
    root = output_root()
    slugs = sorted(p.name for p in root.iterdir() if (p / "model.json").exists()) if root.exists() else []
    migrated, skipped = [], []
    for slug in slugs:
        if store.session_exists(slug):
            skipped.append(slug)
            continue
        store.migrate_legacy(slug)
        migrated.append(slug)
    if a.json:
        _print_json({"migrated": migrated, "skipped_already_present": skipped, "source": str(root)})
        return
    print(f"Legacy sessions under {root}:")
    print(f"  migrated: {', '.join(migrated) or '(none)'}")
    if skipped:
        print(f"  skipped (already in canonical store): {', '.join(skipped)}")
    print("  Legacy files were preserved (read-only).")


def _cmd_session_export(a, client) -> None:
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    if not store.session_exists(slug):
        raise SessionNotFoundError(f"no canonical session '{slug}'", details={"slug": slug})
    d = store.canonical_dir(slug)
    dest = Path(a.output) if a.output else Path.cwd() / f"{slug}.requivo.zip"
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(d.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(d.parent))
    if a.json:
        _print_json({"slug": slug, "archive": str(dest)})
        return
    print(f"Exported session '{slug}' → {dest}")


def _cmd_session_import(a, client) -> None:
    archive = Path(a.archive)
    if not archive.is_file():
        raise SessionNotFoundError(f"archive not found: {archive}", details={"archive": str(archive)})
    root = session_root()
    root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
        # Guard against path traversal (a crafted zip must not escape the sessions root).
        for n in names:
            target = (root / n).resolve()
            if not str(target).startswith(str(root.resolve())):
                raise InvalidModelError(f"unsafe path in archive: {n}")
        z.extractall(root)
    top = sorted({n.split("/")[0] for n in names if "/" in n})
    if a.json:
        _print_json({"imported": top, "into": str(root)})
        return
    print(f"Imported {', '.join(top) or '(nothing)'} → {root}")


# ── model ────────────────────────────────────────────────────────────────────────


def _cmd_model_show(a, client) -> None:
    svc = SessionService()
    model = svc.load_model(svc.resolve_slug(a.session))
    print(model.model_dump_json(indent=2))


def _cmd_model_validate(a, client) -> None:
    """Validate a proposal file — the gate Claude Code runs before applying. On success prints a tiny
    confirmation (or `--json` {status: valid}); on failure the structured error surfaces via app()."""
    data = Path(a.proposal).read_text()
    require = not a.allow_partial
    out = validate_proposal(data, require_complete=require)
    n_slots = len(out.model)
    if a.json:
        _print_json({"status": "valid", "slots": n_slots})
        return
    print(f"✅ Proposal is valid ({n_slots} slots).")


def _cmd_model_apply(a, client) -> None:
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    data = Path(a.proposal).read_text()
    result = svc.update_model(slug, data, require_complete=not a.allow_partial,
                              expected_revision=a.expected_revision,
                              provenance={"provider": "claude-code", "surface": "cli-apply"})
    if a.json:
        _print_json(result.to_dict())
        return
    print(f"✅ Applied → revision {result.revision}")
    print(f"   changed slots: {', '.join(result.changed_slots) or '(none)'}")
    if result.invalidated_decisions:
        print(f"   decisions to re-validate: {len(result.invalidated_decisions)}")
    if result.invalidated_challenges:
        print(f"   premises to re-examine: {len(result.invalidated_challenges)}")
    if result.stale_artifacts:
        print(f"   now stale: {', '.join(result.stale_artifacts)}")
    rd = result.readiness
    print(f"   readiness: {'READY' if rd.ready else 'not ready'}"
          + (f" — blocking: {', '.join(rd.blocking_slots)}" if rd.blocking_slots else ""))


def _cmd_model_diff(a, client) -> None:
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    data = Path(a.proposal).read_text()
    result = svc.diff(slug, data, require_complete=not a.allow_partial)
    if a.json:
        _print_json(result.to_dict())
        return
    print(f"Would apply as revision {result.revision}")
    print(f"  changed slots: {', '.join(result.changed_slots) or '(none)'}")
    if result.stale_artifacts:
        print(f"  would go stale: {', '.join(result.stale_artifacts)}")


# ── artifact ─────────────────────────────────────────────────────────────────────


def _cmd_artifact_save(a, client) -> None:
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    content = Path(a.file).read_text()
    st = ArtifactService().save(slug, a.type, content, source_revision=a.revision)
    if a.json:
        _print_json({"type": a.type, "filename": st.filename, "revision": st.revision})
        return
    print(f"Saved {a.type} → {store.canonical_dir(slug) / 'artifacts' / st.filename} (from revision {st.revision})")


def _cmd_artifact_list(a, client) -> None:
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    items = ArtifactService().list(slug)
    if a.json:
        _print_json(items)
        return
    if not items:
        print(f"No artifacts saved for '{slug}'.")
        return
    print(f"Artifacts for '{slug}':")
    for t, info in items.items():
        print(f"  {t:<12} {info['filename']:<26} rev {info['revision']}  {'STALE' if info['stale'] else 'fresh'}")


def _cmd_artifact_show(a, client) -> None:
    svc = SessionService()
    print(ArtifactService().show(svc.resolve_slug(a.session), a.type))


# ── registration ─────────────────────────────────────────────────────────────────


def register(sub) -> None:
    """Attach the deterministic verb groups to the main `requivo` subparser."""
    # doctor
    dr = sub.add_parser("doctor", help="diagnose the install (no API key needed)")
    dr.add_argument("--json", action="store_true", help="emit the report as JSON")
    dr.set_defaults(func=_cmd_doctor)

    # schema / context — read-only knowledge for a reasoning caller (Claude Code)
    sc = sub.add_parser("schema", help="print the slot schema (the model vocabulary + driver rule)")
    sc.add_argument("--framework", action="store_true", help="also print the human framework spec")
    sc.set_defaults(func=_cmd_schema)

    cx = sub.add_parser("context", help="list or print the product context cards")
    cx.add_argument("--list", action="store_true", help="list available card stems instead of content")
    cx.add_argument("--cards", metavar="CARDS", help="comma-separated subset to print (default: all)")
    cx.set_defaults(func=_cmd_context)

    # session
    sp = sub.add_parser("session", help="create, list, show, migrate, export/import sessions")
    ss = sp.add_subparsers(dest="subcommand", required=True, metavar="<action>")

    si = ss.add_parser("init", help="create a session from a request (no LLM)")
    si.add_argument("request", help="the request, or a path to a file containing it")
    si.add_argument("--slug", help="explicit session slug (default: derived from the request)")
    si.add_argument("--context", metavar="CARDS", help="comma-separated context cards to record")
    si.add_argument("--provider", default=None, help="informational provider tag (e.g. claude-code)")
    si.add_argument("--json", action="store_true")
    si.set_defaults(func=_cmd_session_init)

    sl = ss.add_parser("list", help="list canonical sessions")
    sl.add_argument("--json", action="store_true")
    sl.set_defaults(func=_cmd_session_list)

    sh = ss.add_parser("show", help="show a session's metadata + artifacts")
    sh.add_argument("session", help="session slug or path")
    sh.add_argument("--json", action="store_true")
    sh.set_defaults(func=_cmd_session_show)

    sm = ss.add_parser("migrate", help="migrate ALL legacy out/ sessions into .requivo/sessions/")
    sm.add_argument("--json", action="store_true")
    sm.set_defaults(func=_cmd_session_migrate)

    se = ss.add_parser("export", help="export a session as a .zip archive")
    se.add_argument("session", help="session slug or path")
    se.add_argument("-o", "--output", help="destination archive path")
    se.add_argument("--json", action="store_true")
    se.set_defaults(func=_cmd_session_export)

    sig = ss.add_parser("import", help="import a session archive into the workspace")
    sig.add_argument("archive", help="path to a .zip produced by `session export`")
    sig.add_argument("--json", action="store_true")
    sig.set_defaults(func=_cmd_session_import)

    # model
    mp = sub.add_parser("model", help="show, validate, apply, or diff a model")
    ms = mp.add_subparsers(dest="subcommand", required=True, metavar="<action>")

    msh = ms.add_parser("show", help="print a session's current model")
    msh.add_argument("session", help="session slug or path")
    msh.set_defaults(func=_cmd_model_show)

    mv = ms.add_parser("validate", help="validate a proposal file (no session write)")
    mv.add_argument("proposal", help="path to a proposed model JSON")
    mv.add_argument("--session", help="validate against a session's context (optional)")
    mv.add_argument("--allow-partial", action="store_true",
                    help="do not require the full slot set (partial projection)")
    mv.add_argument("--json", action="store_true")
    mv.set_defaults(func=_cmd_model_validate)

    ma = ms.add_parser("apply", help="validate a proposal and apply it as a new revision")
    ma.add_argument("session", help="session slug or path")
    ma.add_argument("proposal", help="path to a proposed model JSON")
    ma.add_argument("--allow-partial", action="store_true", help="do not require the full slot set")
    ma.add_argument("--expected-revision", type=int, default=None,
                    help="only apply if the session is still at this revision (optimistic lock)")
    ma.add_argument("--json", action="store_true")
    ma.set_defaults(func=_cmd_model_apply)

    md = ms.add_parser("diff", help="show what a proposal would change (no write)")
    md.add_argument("session", help="session slug or path")
    md.add_argument("proposal", help="path to a proposed model JSON")
    md.add_argument("--allow-partial", action="store_true", help="do not require the full slot set")
    md.add_argument("--json", action="store_true")
    md.set_defaults(func=_cmd_model_diff)

    # artifact
    ap = sub.add_parser("artifact", help="save, list, or show generated artifacts")
    aps = ap.add_subparsers(dest="subcommand", required=True, metavar="<action>")

    asv = aps.add_parser("save", help="save an artifact against a session")
    asv.add_argument("session", help="session slug or path")
    asv.add_argument("--type", required=True, choices=sorted(ARTIFACT_FILENAMES),
                     help="artifact type")
    asv.add_argument("--file", required=True, help="path to the artifact content")
    asv.add_argument("--revision", type=int, default=None,
                     help="source model revision (default: the session's current revision)")
    asv.add_argument("--json", action="store_true")
    asv.set_defaults(func=_cmd_artifact_save)

    al = aps.add_parser("list", help="list a session's artifacts + freshness")
    al.add_argument("session", help="session slug or path")
    al.add_argument("--json", action="store_true")
    al.set_defaults(func=_cmd_artifact_list)

    ash = aps.add_parser("show", help="print a saved artifact's content")
    ash.add_argument("session", help="session slug or path")
    ash.add_argument("--type", required=True, choices=sorted(ARTIFACT_FILENAMES))
    ash.set_defaults(func=_cmd_artifact_show)
