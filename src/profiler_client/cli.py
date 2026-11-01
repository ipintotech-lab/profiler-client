"""``profiler-client``: prepare, check and look at a dataset before it is
sent anywhere.

    profiler-client prepare ~/.claude/projects/<project>/<session>.jsonl -o analysis-ready
    profiler-client prepare ~/.codex/sessions/2026/10/18/rollout-*.jsonl -o analysis-ready
    profiler-client inspect analysis-ready
    profiler-client validate analysis-ready
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from . import __version__, policy
from .contract import CONTRACT_VERSION
from .dataset import PrepareError, prepare, summary, validate
from .names import Names
from .pseudonym import Pseudonymizer, load_or_create_key, new_key


def _prepare(args: argparse.Namespace) -> int:
    paths = [Path(p) for p in args.files]
    print("reading:", file=sys.stderr)
    for p in paths:
        print(f"  {p}", file=sys.stderr)
    key = load_or_create_key(Path(args.key_file).expanduser()) if args.key_file else new_key()
    manifest = prepare(paths, Path(args.output), Names(Pseudonymizer(key), keep=args.keep_names))
    print(f"wrote {manifest['events']} events from {manifest['sessions']} session(s) "
          f"({manifest['source_format']}) to {args.output}", file=sys.stderr)
    if manifest["kept_names"]:
        print(f"note: {len(manifest['kept_names'])} name(s) kept in clear text; review them with: "
              f"profiler-client inspect {args.output}", file=sys.stderr)
    if manifest["sessions_without_cost"]:
        print(f"note: {manifest['sessions_without_cost']} session(s) recorded no cost; their cost shows as 0",
              file=sys.stderr)
    if not args.key_file:
        print("note: a fresh key was used and not kept, so this dataset can't be linked to any other", file=sys.stderr)
    return 0


def _validate(args: argparse.Namespace) -> int:
    manifest, _ = validate(Path(args.dataset))
    print(f"ok: {manifest['events']} events, contract v{manifest['contract_version']}, "
          f"sha256 {manifest['payload_sha256']}")
    return 0


def _inspect(args: argparse.Namespace) -> int:
    manifest, events = validate(Path(args.dataset))
    print(json.dumps({"manifest": manifest, **summary(events)}, indent=2, ensure_ascii=False))
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="profiler-client", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("prepare", help="convert and pseudonymize transcripts into a dataset directory")
    p.add_argument("files", nargs="+", help="trace files: Claude Code or Codex CLI sessions, OpenTelemetry OTLP JSON, "
                                            "OpenAI chat trajectories or your own event log (JSON or JSONL); "
                                            "directories and symlinks are refused")
    p.add_argument("-o", "--output", required=True, help="new or empty directory to write the dataset to")
    p.add_argument("--key-file", help="keep and reuse a key here (mode 0600), to compare datasets over time")
    p.add_argument("--keep-names", action="store_true",
                   help="keep your workflow, agent, node and tool names readable (single words only); "
                        "inspect lists every kept name")
    p.set_defaults(run=_prepare)

    v = sub.add_parser("validate", help="check a dataset: hash, privacy lint and contract")
    v.add_argument("dataset")
    v.set_defaults(run=_validate)

    i = sub.add_parser("inspect", help="validate a dataset and show what it contains")
    i.add_argument("dataset")
    i.set_defaults(run=_inspect)

    sub.add_parser("version", help="print client, contract and policy versions").set_defaults(run=_version)

    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except (PrepareError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
