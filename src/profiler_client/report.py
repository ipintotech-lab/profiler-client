"""Put your own names back into a report from the AI Agent Profiler.

``prepare`` keeps a name list on this machine -- each pseudonym of a
workflow, agent, step, tool or model name next to the name -- in
``$XDG_DATA_HOME/profiler-client/names/`` (``~/.local/share/...``), mode
0600, outside the dataset directory, so it is never uploaded. It holds
names, plus the values of event-log input fields that take only a few
short values (``Names.field_values``); outputs, ids, prompts and any
other input are never written.

``open`` reads a downloaded report JSON, replaces every pseudonym it finds
in the name lists, and writes a self-contained HTML page you can read or
print to PDF. Nothing is sent anywhere. A pseudonym is a keyed hash, so
one from another dataset never matches by mistake.
"""
from __future__ import annotations

import html
import json
import os
import re
from pathlib import Path
from typing import Any

from .pseudonym import TOKEN

NAME_LIST_VERSION = 1


def name_list_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "profiler-client" / "names"


def write_private(directory: Path, payload_sha256: str, data: dict) -> Path:
    """Write ``<payload_sha256>.json`` in ``directory``, owner-only (0600)."""
    if not re.fullmatch(r"[a-f0-9]{64}", payload_sha256):
        raise ValueError("payload_sha256 must be a lowercase SHA-256 hex digest")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{payload_sha256}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    if hasattr(os, "fchmod"):
        os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, sort_keys=True)
    return path


def write_name_list(payload_sha256: str, names: dict[str, str], directory: Path | None = None) -> Path:
    return write_private(directory or name_list_dir(), payload_sha256,
                         {"version": NAME_LIST_VERSION, "payload_sha256": payload_sha256, "names": names})


def read_name_lists(directory: Path | None = None, payload_sha256: str | None = None) -> dict[str, str]:
    """The name mapping for one payload, by its hash.

    Without a valid ``payload_sha256`` this returns nothing: a report that
    carries no hash is never matched against the name lists of *other*
    datasets. (Earlier this merged every list on the machine when no hash
    was given, which could put one dataset's names into another's report.)
    """
    directory = directory or name_list_dir()
    names: dict[str, str] = {}
    if not isinstance(payload_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", payload_sha256):
        return names
    try:
        data = json.loads((directory / f"{payload_sha256}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return names
    if (isinstance(data, dict) and isinstance(data.get("names"), dict)
            and data.get("payload_sha256") == payload_sha256):
        names.update({k: v for k, v in data["names"].items()
                      if isinstance(k, str) and isinstance(v, str) and TOKEN.fullmatch(k)})
    return names


def forget_name_lists(directory: Path | None = None, payload_sha256: str | None = None,
                      *, everything: bool = False) -> int:
    """Delete name lists on this machine and return how many were removed:
    all of them with ``everything``, or the one for ``payload_sha256``."""
    directory = directory or name_list_dir()
    if not directory.is_dir():
        return 0
    if everything:
        paths = sorted(directory.glob("*.json"))
    elif isinstance(payload_sha256, str) and re.fullmatch(r"[a-f0-9]{64}", payload_sha256):
        path = directory / f"{payload_sha256}.json"
        paths = [path] if path.exists() else []
    else:
        return 0
    for path in paths:
        path.unlink()
    return len(paths)


def translate(value: Any, names: dict[str, str], found: set[str], missing: set[str]) -> Any:
    """``value`` with every known pseudonym replaced, in strings and in keys."""
    def sub(match) -> str:
        token = match.group(0)
        if token in names:
            found.add(token)
            return names[token]
        missing.add(token)
        return token

    if isinstance(value, str):
        return TOKEN.sub(sub, value)
    if isinstance(value, list):
        return [translate(v, names, found, missing) for v in value]
    if isinstance(value, dict):
        return {translate(k, names, found, missing): translate(v, names, found, missing) for k, v in value.items()}
    return value


# --- the HTML page: drawn by report.js, the same renderer as the web report ---

STATIC = Path(__file__).resolve().parent / "static"


def _script_json(value: Any) -> str:
    """JSON that can sit inside a <script> element: no "<" can close it early."""
    return (json.dumps(value, ensure_ascii=False)
            .replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def _pick(value: Any, keys: tuple[str, ...]) -> dict:
    return {k: value[k] for k in keys if k in value} if isinstance(value, dict) else {}


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def view(report: dict) -> dict:
    """Only the fields report.js draws. The page holds restored names, and a finding's
    matched inputs (``inputs``, ``groups``) are never drawn, so they are never written.
    A dataset overview keeps its rows and each workflow's report, each viewed the same way."""
    overview, reports = report.get("overview"), report.get("reports")
    if isinstance(overview, dict) and isinstance(reports, dict):
        return {
            "overview": {
                "workflows": [_pick(r, ("name", "runs", "spend", "replaceable", "share", "findings", "highest_risk"))
                              for r in _list(overview.get("workflows")) if isinstance(r, dict)],
                "totals": _pick(overview.get("totals"), ("spend", "replaceable", "share", "findings")),
                **_pick(overview, ("risk_labels",)),
            },
            "reports": {name: _workflow_view(r) for name, r in reports.items() if isinstance(r, dict)},
            "grade": _pick(report.get("grade"), ("grade", "word")) or None,
        }
    return _workflow_view(report)


def _workflow_view(report: dict) -> dict:
    summary = _pick(report.get("summary"), ("totals", "notes", "replaceable", "risk_labels"))
    if "next_steps" in _pick(report.get("summary"), ("next_steps",)):
        summary["next_steps"] = [
            _pick(n, ("step", "do", "type", "title", "nodes", "saving", "risk", "risk_reasons", "observations"))
            for n in _list(report["summary"]["next_steps"]) if isinstance(n, dict)]
    summary["opportunities"] = [
        {**_pick(o, ("type", "nodes", "title", "saving", "observations", "action")),
         "identity": _pick(o.get("identity"), ("agent",))}
        for o in _list(_pick(report.get("summary"), ("opportunities",)).get("opportunities")) if isinstance(o, dict)]
    evidence = ("occurrences", "unique_inputs", "repeated_inputs", "repeated_observations", "determinism_confidence")
    by_agent = _pick(report.get("mine"), ("by_agent",)).get("by_agent")
    mine = {"by_agent": {agent: {"actions": [
        {**_pick(a, ("node", "occurrences", "total_cost", "classification")), "evidence": _pick(a.get("evidence"), evidence)}
        for a in _list(_pick(by, ("actions",)).get("actions")) if isinstance(a, dict)]}
        for agent, by in (by_agent.items() if isinstance(by_agent, dict) else ())}}
    observe = _pick(report.get("observe"), ("selected_workflow",))
    observe["workflows"] = [_pick(w, ("name", "num_runs")) for w in _list(_pick(report.get("observe"), ("workflows",)).get("workflows"))]
    graph = report.get("graph")
    return {
        "mode": report.get("mode"),
        "summary": summary,
        "mine": mine,
        "observe": observe,
        "grade": _pick(report.get("grade"), ("grade", "word")) or None,
        "availability": {"graph": _pick(_pick(report.get("availability"), ("graph",)).get("graph"), ("available", "code")) or None},
        "graph": {
            "run_count": graph.get("run_count"),
            "nodes": [_pick(n, ("id", "kind", "agent", "occurrences", "cost")) for n in _list(graph.get("nodes"))],
            "edges": [{**_pick(e, ("from", "to", "kind", "transition_count", "distinct_run_count")),
                       # only the event ids, for show-event: a pseudonym never restored, nothing of the call
                       "evidence_sample": [{"event_id": x["event_id"]} for x in _list(e.get("evidence_sample"))
                                           if isinstance(x, dict) and TOKEN.fullmatch(str(x.get("event_id")))]}
                      for e in _list(graph.get("edges"))],
        } if isinstance(graph, dict) else None,
        "data_quality": {"gaps": [_pick(g, ("key", "label", "effect"))
                                  for g in _list(_pick(report.get("data_quality"), ("gaps",)).get("gaps"))]},
    }


def render_html(report: dict, title: str, restored: int | None = None, unmatched: int | None = None,
                source: str | None = None) -> str:
    # be honest in the header about how much was restored: with nothing matched,
    # don't claim "your own names" -- the page is still pseudonymized
    if restored:
        meta = ["Opened on your machine with your own names"]
        if unmatched:
            meta.append(f"{unmatched:,} pseudonym{'' if unmatched == 1 else 's'} not matched")
    elif unmatched:
        meta = ["Opened on your machine", "no names restored, pseudonyms shown"]
    else:
        meta = ["Opened on your machine"]
    css = (STATIC / "report.css").read_text(encoding="utf-8")
    js = (STATIC / "report.js").read_text(encoding="utf-8")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(TOKEN.sub(lambda m: m.group(0)[:9], title))}</title>
<style>
  body {{ margin: 0; background: #f4f5f8; }}
  main {{ max-width: 1000px; margin: 0 auto; padding: 32px 16px 48px; }}
  .btn {{ background: transparent; color: #3a3e9e; border: 0; box-shadow: inset 0 0 0 1.5px #3a3e9e; border-radius: 6px;
         padding: 6px 12px; font: 600 14px/1.2 "Helvetica Neue", Arial, sans-serif; cursor: pointer; }}
  @media print {{ body {{ background: #fff; }} main {{ padding: 0; max-width: none; }} }}
{css}
</style></head>
<body><main>
<article class="sheet" id="report"><noscript>This report is drawn by a script in this file. Allow scripts for this local page to see it.</noscript></article>
</main>
<script type="application/json" id="report-data">{_script_json({"report": view(report), "name": title, "meta": meta, "source": source})}</script>
<script>
{js}
</script>
<script>
(function () {{
  const page = JSON.parse(document.getElementById('report-data').textContent);
  const root = document.getElementById('report');
  ProfilerReport.render(root, page.report, {{ name: page.name, meta: page.meta, showEvent: page.source }});
  const print = document.createElement('button');
  print.className = 'btn';
  print.type = 'button';
  print.textContent = 'Print or save as PDF';
  print.addEventListener('click', () => window.print());
  root.querySelector('.tools').append(print);
}})();
</script>
</body></html>
"""
