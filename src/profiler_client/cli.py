"""``profiler-client``: prepare, check and look at a dataset before it is
sent anywhere.

    profiler-client prepare ~/.claude/projects/<project>/<session>.jsonl -o analysis-ready
    profiler-client prepare ~/.codex/sessions/2026/10/18/rollout-*.jsonl -o analysis-ready
    profiler-client check ~/traces/*.jsonl --prices 1.25,10
    profiler-client inspect analysis-ready
    profiler-client validate analysis-ready
    profiler-client show-event report.json h:<event id>
    profiler-client app   # a page on 127.0.0.1: choose files, prepare, send, see the report
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

from . import __version__, policy
from . import prices as token_prices
from .contract import CONTRACT_VERSION, TraceEvent
from .dataset import EVENTS, PrepareError, convert_file, prepare, read_sealed, subagents_across, summary, validate
from .keyfile import key_from_command, key_from_env
from .names import Names
from .pseudonym import TOKEN, Pseudonymizer, load_or_create_key, new_key
from .readiness import is_llm, readiness, remediation, workflows
from .report import forget_name_lists, read_name_lists, render_html, translate, write_name_list
from .trace_refs import original_record, read_refs, refs_dir, write_refs

# a decoded report is local and never huge; bound it so a malformed or hostile
# file fails cleanly instead of exhausting memory or the recursion stack
MAX_REPORT_BYTES = 64 * 1024 * 1024
HEX64 = re.compile(r"[a-f0-9]{64}")
DEFAULT_SERVER = "https://openly7.com"


def prepare_dataset(paths: list[Path], output: Path, *, keep_names: bool = False, llm_inputs: bool = False,
                    key_file: str | None = None, name_list: bool = True,
                    key: bytes | None = None) -> tuple[dict, list[dict]]:
    """``prepare``: write the dataset to ``output`` and, unless
    ``name_list`` is False, keep its name list and event references on this
    machine. Returns the manifest and the events. The key is ``key``, else
    the one kept in ``key_file``, else a fresh one."""
    if key is None:
        key = load_or_create_key(Path(key_file).expanduser()) if key_file else new_key()
    names = Names(Pseudonymizer(key), keep=keep_names, llm_inputs=llm_inputs)
    refs = {} if name_list else None
    manifest = prepare(paths, output, names, refs)
    seal, events = read_sealed((output / EVENTS).read_bytes())
    if refs is not None:
        write_name_list(seal["payload_sha256"], {**names.seen, **names.field_values()})
        write_refs(seal["payload_sha256"], refs)
    return manifest, events


def prepare_check(manifest: dict, events: list[dict], files: int) -> str:
    """The ``check`` text for a dataset just prepared from ``files`` files."""
    report = readiness([TraceEvent.model_validate(e) for e in events])
    return _check_text({"readable": {"files": files, "read": files, "skipped": [],
                                     "formats": dict.fromkeys(manifest["source_format"].split("+"), 1)},
                        **report, "remediation": remediation(report, prices=False)})


def _note(text: str) -> None:
    print(f"note: {text}", file=sys.stderr)


def _warn(text: str) -> None:
    print(f"warning: {text}", file=sys.stderr)


def _given_key(args: argparse.Namespace) -> bytes | None:
    """The key from --key-env, --key-command or --key-file (at most one can
    be given), or None for a fresh key."""
    if args.key_env:
        return key_from_env(args.key_env)
    if args.key_command:
        return key_from_command(args.key_command)
    if args.key_file:
        return load_or_create_key(Path(args.key_file).expanduser(), note=_note, warn=_warn)
    return None


def _prepare(args: argparse.Namespace) -> int:
    paths = [Path(p) for p in args.files]
    key = _given_key(args)
    print("reading:", file=sys.stderr)
    for p in paths:
        print(f"  {p}", file=sys.stderr)
    manifest, events = prepare_dataset(paths, Path(args.output), keep_names=args.keep_names,
                                       llm_inputs=args.llm_inputs, key=key,
                                       name_list=not args.no_name_list)
    print(f"wrote {manifest['events']} events from {manifest['sessions']} session(s) "
          f"({manifest['source_format']}) to {args.output}", file=sys.stderr)
    if manifest["kept_names"]:
        print(f"note: {len(manifest['kept_names'])} name(s) kept in clear text; review them with: "
              f"profiler-client inspect {args.output}", file=sys.stderr)
    if manifest["sessions_without_cost"]:
        print(f"note: {manifest['sessions_without_cost']} session(s) recorded no cost; their cost shows as 0",
              file=sys.stderr)
    print("\n" + prepare_check(manifest, events, len(paths)), file=sys.stderr)
    if key is None:
        print("note: a fresh key was used and not kept, so this dataset can't be linked to any other", file=sys.stderr)
    return 0

GRADE_ORDER = "ABCD"


def _unpriced_models(events: list, prices: token_prices.Prices, names: Names) -> list[str]:
    """The models of LLM calls with tokens and no cost that no price covers,
    as the trace names them."""
    keyed = token_prices.keyed(prices, names.model)
    models = {e.model for e in events if is_llm(e) and e.cost <= 0 and e.tokens.input + e.tokens.output > 0}
    return sorted(str(names.seen.get(m, m) if m else "(no model)") for m in models
                  if token_prices.price_for(keyed, m) is None)


def _check(args: argparse.Namespace) -> int:
    """Convert in memory and say what the analysis can find. Writes nothing,
    keeps no key or name list, opens no connection."""
    prices = token_prices.parse(args.prices, args.prices_file)
    names = Names(Pseudonymizer(new_key()))
    events, formats, skipped = [], Counter(), []
    paths = [Path(p) for p in args.files]
    subagents = subagents_across(paths)
    for path in paths:
        try:
            source, conversion = convert_file(path, names, subagents)
        except PrepareError as exc:
            skipped.append({"file": path.name if args.names else None, "code": exc.code})
            continue
        formats[source] += 1
        events.extend(TraceEvent.model_validate(e) for e in conversion.events)
    readable = {"files": len(args.files), "read": sum(formats.values()), "formats": dict(formats),
                "skipped": skipped}
    if not events:
        out = {"readable": readable}
        print(json.dumps(out, indent=2) if args.json else
              f"Read none of {len(args.files)} file(s): " + ", ".join(s["code"] for s in skipped), file=sys.stdout)
        return 2
    unpriced = _unpriced_models(events, prices, names) if prices else []
    priced = prices is not None and not unpriced
    report = readiness(events, prices=priced)
    if args.names:
        for w, name in zip(report["workflows"], (f["name"] for f in workflows(events))):
            w["name"] = names.seen.get(name, name)
    out = {"readable": readable, **report, "remediation": remediation(report, priced)}
    if unpriced:  # model names only with --names: without it the output holds only counts
        out["unpriced_models"] = unpriced if args.names else len(unpriced)
        which = ("model " + ", ".join(unpriced)) if args.names else \
            f"{len(unpriced)} model{'' if len(unpriced) == 1 else 's'} (--names lists them)"
        out["remediation"].append(f"No price for {which}: add --prices MODEL=IN,OUT for each, "
                                  "or a default --prices IN,OUT.")
    print(json.dumps(out, indent=2, ensure_ascii=False) if args.json else _check_text(out))
    if args.min_grade and any(GRADE_ORDER.index(w["grade"]["grade"]) > GRADE_ORDER.index(args.min_grade)
                              for w in report["workflows"]):
        return 1
    return 0


def _check_text(out: dict) -> str:
    r, d = out["readable"], out["dataset"]
    lines = [(f"Read {r['read']} of {r['files']} file(s) ({', '.join(sorted(r['formats']))}). "
              f"{d['events']:,} events, {d['workflows']} workflow{'' if d['workflows'] == 1 else 's'}.")]
    if r["skipped"]:
        lines.append("Skipped: " + ", ".join(f"{s['file'] or 'a file'} ({s['code']})" for s in r["skipped"]))
    g = d["grade"]
    lines += ["", f"Dataset grade: {g['grade']} ({g['word']}, {g['score']}/100)"]
    for c in g["checks"]:
        lines.append(f"  {c['label']:<24} {c['status']:<8} {c['detail']}")
    lines += ["", "What the analysis can find"]
    for f in out["findings_possible"]:
        lines.append(f"  {f['label']:<37} {f['status']:<8} {f['reason']}")
    if len(out["workflows"]) > 1:
        lines += ["", "Workflows"]
        for w in out["workflows"]:
            label = w.get("name") or f"workflow {w['index']}"
            lines.append(f"  {label:<37} {w['grade']['grade']}  {w['runs']} runs, {w['events']:,} events")
    if out["remediation"]:
        lines += ["", "To get more"] + [f"  {line}" for line in out["remediation"]]
    return "\n".join(lines)


def _validate(args: argparse.Namespace) -> int:
    manifest, _ = validate(Path(args.dataset))
    print(f"ok: {manifest['events']} events, contract v{manifest['contract_version']}, "
          f"sha256 {manifest['payload_sha256']}")
    return 0


def _inspect(args: argparse.Namespace) -> int:
    manifest, events = validate(Path(args.dataset))
    # the dataset-level part of ``check``; the raw-level part needs the raw files
    ready = readiness([TraceEvent.model_validate(e) for e in events]) if events else None
    print(json.dumps({"manifest": manifest, **summary(events), "readiness": ready}, indent=2, ensure_ascii=False))
    return 0


def _dataset_hashes(report: dict) -> list[str]:
    """The datasets a report was made from: ``payload_sha256s`` for a
    report over several, else ``payload_sha256``."""
    many = report.get("payload_sha256s")
    if isinstance(many, list) and many and all(isinstance(h, str) and HEX64.fullmatch(h) for h in many):
        return many
    one = report.get("payload_sha256")
    return [one] if isinstance(one, str) else []


def _read_report(source: Path) -> dict:
    return parse_report(source.read_bytes())


def parse_report(raw: bytes) -> dict:
    """A report JSON's object, refusing one too large or deep to read."""
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError(f"report is larger than {MAX_REPORT_BYTES} bytes")
    try:
        report = json.loads(raw.decode("utf-8"))
    except RecursionError:
        raise ValueError("report is nested too deeply to read") from None
    if not isinstance(report, dict):
        raise TypeError("report JSON must contain an object")
    return report


def restore_names(report: dict) -> tuple[dict, set[str], set[str], list[str], list[str]]:
    """The report with the names this machine's name lists hold put back:
    ``(report, found, missing, hashes, unlisted)`` -- the pseudonyms
    restored and not, the report's dataset hashes, and those with no name
    list here."""
    hashes = _dataset_hashes(report)
    found: set[str] = set()
    missing: set[str] = set()
    names: dict[str, str] = {}
    unlisted = []
    for digest in hashes:
        listed = read_name_lists(payload_sha256=digest)
        if not listed:
            unlisted.append(digest)
        names.update(listed)
    try:
        report = translate(report, names, found, missing)
    except RecursionError:
        raise ValueError("report is nested too deeply to read") from None
    return report, found, missing, hashes, unlisted


def report_title(report: dict, default: str) -> str:
    observe = report.get("observe")
    return (observe.get("selected_workflow") if isinstance(observe, dict) else None) or default


def _open(args: argparse.Namespace) -> int:
    source = Path(args.report)
    report, found, missing, hashes, unlisted = restore_names(_read_report(source))
    title = report_title(report, source.stem)
    output = Path(args.output) if args.output else source.with_suffix(".html")
    replaced = output.exists()
    # the decoded page holds your real values; keep it owner-only (0600),
    # and force that even if the path already existed with wider permissions
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    if hasattr(os, "fchmod"):
        os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(render_html(report, title, restored=len(found), unmatched=len(missing),
                            source=str(source.resolve())))
    # say plainly what was and wasn't restored, so a half-restored report isn't mistaken for a full one
    if not hashes:
        print("this report has no dataset hash (payload_sha256), so names were not restored")
    for digest in unlisted:
        print(f"no name list on this machine for dataset {digest[:12]}…, so its names were not restored")
    print(f"restored {len(found)} name(s); {len(missing)} pseudonym(s) had no match")
    print(f"{'replaced' if replaced else 'wrote'} {output}")
    return 0


def _show_event(args: argparse.Namespace) -> int:
    if not TOKEN.fullmatch(args.event_id):
        raise ValueError("an event id looks like h: and 64 hex digits, as in the report")
    hashes = [d for d in _dataset_hashes(_read_report(Path(args.report))) if HEX64.fullmatch(d)]
    if not hashes:
        raise ValueError("this report has no dataset hash (payload_sha256), so its events can't be found")
    found_refs = [refs for refs in map(read_refs, hashes) if refs is not None]
    if not found_refs:
        raise ValueError("this machine has no event references for this report's dataset(s); "
                         "they are kept by prepare unless --no-name-list was given")
    for refs in found_refs:
        if args.event_id in refs["events"]:
            print(json.dumps(original_record(refs, args.event_id), indent=2, ensure_ascii=False))
            return 0
    raise ValueError("that event isn't in the event references of this report's dataset(s)")


def _forget_one(payload_sha256: str | None = None, everything: bool = False) -> str:
    names = forget_name_lists(payload_sha256=payload_sha256, everything=everything)
    refs = forget_name_lists(refs_dir(), payload_sha256=payload_sha256, everything=everything)
    return f"removed {names} name list(s) and {refs} event reference file(s)"


def _forget(args: argparse.Namespace) -> int:
    if args.all:
        print(_forget_one(everything=True))
        return 0
    if not args.target:
        raise ValueError("give a report JSON or a dataset hash, or use --all")
    if HEX64.fullmatch(args.target):
        digests = [args.target]
    else:
        report = json.loads(Path(args.target).read_bytes().decode("utf-8"))
        digests = [d for d in _dataset_hashes(report) if HEX64.fullmatch(d)] if isinstance(report, dict) else []
        if not digests:
            raise ValueError("that report has no dataset hash (payload_sha256) to forget")
    for digest in digests:
        print(f"{_forget_one(digest)} for dataset {digest[:12]}…")
    return 0


def _app(args: argparse.Namespace) -> int:
    from .app import serve  # the HTTP modules load only for this command
    serve(args.server, port=args.port, open_browser=not args.no_browser)
    return 0


def _version(_: argparse.Namespace) -> int:
    print(f"profiler-client {__version__}")
    print(f"contract v{CONTRACT_VERSION}, policy v{policy.POLICY_VERSION}")
    # hash of the installed package source, so a tester can report exactly what ran
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    print(f"source sha256 {digest.hexdigest()}")
    return 0


def _utf8_output() -> None:
    """Print UTF-8 whatever the console's code page. On Windows, output sent
    to a pipe or file otherwise uses the locale's code page (cp1252, cp1255,
    ...), which can't encode names like ``llm_call ← Bash`` and fails."""
    for stream in (sys.stdout, sys.stderr):
        if (getattr(stream, "encoding", None) or "").lower().replace("-", "") != "utf8" \
                and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    _utf8_output()
    parser = argparse.ArgumentParser(prog="profiler-client", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("prepare", help="convert and pseudonymize transcripts into a dataset directory")
    p.add_argument("files", nargs="+", help="trace files: Claude Code or Codex CLI sessions, OpenTelemetry OTLP JSON, "
                                            "OpenAI chat trajectories or your own event log (JSON or JSONL); "
                                            "directories and symlinks are refused")
    p.add_argument("-o", "--output", required=True, help="new or empty directory to write the dataset to")
    keys = p.add_mutually_exclusive_group()
    keys.add_argument("--key-file", help="keep and reuse a key here, to compare datasets over time (mode 0600; "
                                         "on Windows, encrypted for your Windows account)")
    keys.add_argument("--key-env", metavar="VAR", help="read the key, in hex, from this environment variable")
    keys.add_argument("--key-command", metavar="COMMAND",
                      help="run this command (no shell) and read the key, in hex, from its output, e.g. from a "
                           "secret store")
    p.add_argument("--keep-names", action="store_true",
                   help="keep your workflow, agent, node and tool names readable (single words only); "
                        "inspect lists every kept name")
    p.add_argument("--llm-fingerprint", "--llm-inputs", dest="llm_inputs", action="store_true",
                   help="include a fingerprint of what the model read (no text is sent): for each LLM call, one "
                        "pseudonym of everything the model read so far, so the report can tell when a model call "
                        "repeats (Claude Code, Codex and chat trajectories; --llm-inputs is the old name)")
    p.add_argument("--no-name-list", action="store_true",
                   help="don't keep pseudonym-to-name mappings or event references (for show-event) on this machine")
    p.set_defaults(run=_prepare)

    c = sub.add_parser("check", help="say what the analysis can find in raw traces, before preparing; "
                                     "writes nothing and opens no connection")
    c.add_argument("files", nargs="+", help="trace files, as for prepare; directories and symlinks are refused")
    c.add_argument("--prices", action="append", metavar="[MODEL=]IN,OUT",
                   help="$ per 1M input and output tokens, to estimate missing cost: IN,OUT for every model, "
                        "MODEL=IN,OUT for one model (repeat it for each)")
    c.add_argument("--prices-file", metavar="FILE",
                   help='JSON prices per model, e.g. {"gpt-5": [1.25, 10], "*": [3, 15]}; "*" is every other model')
    c.add_argument("--json", action="store_true", help="print one JSON object")
    c.add_argument("--names", action="store_true", help="list workflow and file names (left out by default)")
    c.add_argument("--min-grade", choices=list(GRADE_ORDER), help="exit 1 when any workflow grades below this")
    c.set_defaults(run=_check)

    v = sub.add_parser("validate", help="check a dataset: hash, privacy lint and contract")
    v.add_argument("dataset")
    v.set_defaults(run=_validate)

    i = sub.add_parser("inspect", help="validate a dataset and show what it contains")
    i.add_argument("dataset")
    i.set_defaults(run=_inspect)

    o = sub.add_parser("open", help="open a downloaded report JSON with your names restored")
    o.add_argument("report", help="downloaded report JSON")
    o.add_argument("-o", "--output", help="HTML output path (default: beside the JSON)")
    o.set_defaults(run=_open)

    se = sub.add_parser("show-event", help="print an event's original record from your trace file, on this machine")
    se.add_argument("report", help="downloaded report JSON")
    se.add_argument("event_id", help="the event id from the report (h:...)")
    se.set_defaults(run=_show_event)

    fg = sub.add_parser("forget", help="delete the local pseudonym-to-name list(s) and event references")
    fg.add_argument("target", nargs="?", help="a report JSON, or a dataset hash (payload_sha256)")
    fg.add_argument("--all", action="store_true", help="delete every name list and event reference on this machine")
    fg.set_defaults(run=_forget)

    ap = sub.add_parser("app", help="open a page on this machine to choose trace files, prepare them, send them to "
                                    "Openly7 and see the report with your names restored")
    ap.add_argument("--server", default=DEFAULT_SERVER, help=f"the Openly7 site to send to (default {DEFAULT_SERVER})")
    ap.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: any free one)")
    ap.add_argument("--no-browser", action="store_true", help="print the page's address instead of opening it")
    ap.set_defaults(run=_app)
    sub.add_parser("version", help="print client, contract and policy versions").set_defaults(run=_version)

    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except (PrepareError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
