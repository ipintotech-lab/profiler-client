"""Put your own names back into a report from the AI Agent Profiler.

``prepare`` keeps a name list on this machine -- each pseudonym of a
workflow, agent, step, tool or model name next to the name -- in
``$XDG_DATA_HOME/profiler-client/names/`` (``~/.local/share/...``), mode
0600, outside the dataset directory, so it is never uploaded. It holds
names only: inputs, outputs, ids and prompts are never written.

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


def write_name_list(payload_sha256: str, names: dict[str, str], directory: Path | None = None) -> Path:
    directory = directory or name_list_dir()
    if not re.fullmatch(r"[a-f0-9]{64}", payload_sha256):
        raise ValueError("payload_sha256 must be a lowercase SHA-256 hex digest")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{payload_sha256}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    if hasattr(os, "fchmod"):
        os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"version": NAME_LIST_VERSION, "payload_sha256": payload_sha256, "names": names},
                  f, ensure_ascii=False, sort_keys=True)
    return path


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


# --- the HTML page: the same table as the web report ---

KIND = {
    "CACHE_RESULT": "Same call, same input, same result",
    "EXTRACT_DETERMINISTIC_SUBSTEP": "Same steps in the same order",
    "CONDITIONAL_LLM_BYPASS": "An LLM choice that never changes",
    "REVIEW_HIDDEN_STATE": "Same request, different results",
}
MORE_RUNS = ("Send more runs of this workflow, so the same inputs come up again and the report "
             "can tell whether this step repeats.")


def _text(value: Any) -> str:
    """Escaped text; a pseudonym no name list knows shows as its first 7 characters."""
    return TOKEN.sub(lambda m: m.group(0)[2:9], html.escape(str(value)))


def _money(x: Any) -> str:
    x = float(x or 0)
    if not x:
        return "$0"
    if x < 0.001:
        return "&lt;$0.001"
    return f"${x:.3f}" if x < 1 else f"${x:,.2f}"


def _share(part: float, whole: float) -> str:
    return f"{round(100 * part / whole)}%" if whole else ""


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _steps(report: dict) -> list[dict]:
    opps = (report.get("summary") or {}).get("opportunities") or []
    steps = [{**a, "agent": agent, "finds": []}
             for agent, by in ((report.get("mine") or {}).get("by_agent") or {}).items()
             for a in by.get("actions") or []]
    for o in opps:
        node, agent = (o.get("nodes") or [o.get("title")])[0], (o.get("identity") or {}).get("agent")
        row = next((s for s in steps if s["node"] == node and (not agent or s["agent"] == agent)), None) \
            or next((s for s in steps if s["node"] == node), None)
        if row:
            row["finds"].append(o)
    for s in steps:
        s["repeat"] = sum(o.get("saving") or 0 for o in s["finds"] if o["type"] == "CACHE_RESULT")
    steps.sort(key=lambda s: (-(s.get("total_cost") or 0), -len(s["finds"])))
    return steps


def _repeats(s: dict) -> str:
    lines = []
    cache = [o for o in s["finds"] if o["type"] == "CACHE_RESULT"]
    if cache:
        calls = sum(o.get("observations") or 0 for o in cache)
        lines.append(f"{calls - len(cache):,} of {_plural(calls, 'call')}, {_money(s['repeat'])}"
                     + (f'<span class="note">across {len(cache)} different inputs</span>' if len(cache) > 1 else ""))
    for kind, label in KIND.items():
        found = [o for o in s["finds"] if o["type"] == kind and kind != "CACHE_RESULT"]
        if found:
            saving = sum(o.get("saving") or 0 for o in found)
            lines.append(label + (f" ({len(found)})" if len(found) > 1 else "") + (f", {_money(saving)}" if saving else ""))
    if lines:
        return "".join(f'<div class="find">{line}</div>' for line in lines)
    return '<span class="muted">' + ("Can’t tell yet" if s.get("classification") == "INSUFFICIENT_EVIDENCE"
                                     else "None") + "</span>"


def _tries(s: dict, cost: float) -> list[str]:
    acts: list[str] = []
    for o in sorted(s["finds"], key=lambda o: list(KIND).index(o["type"]) if o["type"] in KIND else len(KIND)):
        act = _text(o.get("action") or "")
        if o["type"] == "EXTRACT_DETERMINISTIC_SUBSTEP":
            act += ": " + " → ".join(f'<span class="step">{_text(n)}</span>' for n in o.get("nodes") or [])
        if act not in acts:
            acts.append(act)
    if not acts and s.get("classification") == "INSUFFICIENT_EVIDENCE" and cost and (s.get("total_cost") or 0) / cost >= .05:
        acts.append(html.escape(MORE_RUNS))
    return acts


def render_html(report: dict, title: str, restored: int | None = None, unmatched: int | None = None) -> str:
    summary = report.get("summary") or {}
    t = summary.get("totals") or {}
    cost = t.get("cost") or 0
    steps = _steps(report)
    listed = [s for s in steps if (s.get("total_cost") or 0) > 0 or s["finds"]]
    biggest = max([s.get("total_cost") or 0 for s in steps] or [0])
    repeated = sum(s["repeat"] for s in steps)

    workflows = (report.get("observe") or {}).get("workflows") or []
    selected = (report.get("observe") or {}).get("selected_workflow")
    wf = next((w for w in workflows if w.get("name") == selected), None)
    scope = ""
    if wf and len(workflows) > 1:
        runs = sum(w.get("num_runs") or 0 for w in workflows)
        scope = (f'<p class="scope">This report covers one workflow, <span class="step">{_text(wf["name"])}</span>: '
                 f'{_plural(wf.get("num_runs") or 0, "run")} of the {runs:,} in your dataset.</p>')

    lead = [f"This workflow spent <b>{_money(cost)}</b>.",
            f"<b>{_money(repeated)}</b> of it ({_share(repeated, cost)}) went on repeats." if repeated
            else "None of it went on repeated calls the report could see."]
    if len(listed) > 1 and cost and (listed[0].get("total_cost") or 0) / cost >= .3:
        lead.append(f'<span class="step">{_text(listed[0]["node"])}</span> alone took '
                    f'{_share(listed[0]["total_cost"], cost)}.')

    rows = []
    for s in steps:
        acts = _tries(s, cost)
        width = lambda v: f"{100 * v / biggest:.1f}" if biggest else "0"
        rows.append(
            f'<tr class="{"has-try " if acts else ""}{"" if s in listed else "free"}">'
            f'<td><span class="step">{_text(s["node"])}</span><span class="note">{_plural(s.get("occurrences") or 0, "call")}</span></td>'
            f'<td class="barcell"><div class="spendbar"><i style="width:{width(s.get("total_cost") or 0)}%"></i>'
            f'<i class="rep" style="width:{width(s["repeat"])}%"></i></div></td>'
            f'<td class="num">{_money(s.get("total_cost"))}<span class="note">{_share(s.get("total_cost") or 0, cost)}</span></td>'
            f'<td>{_repeats(s)}</td></tr>'
            + ('<tr class="try"><td colspan="4">' + "<br>".join(f"<strong>Try:</strong> {a}" for a in acts) + "</td></tr>"
               if acts else ""))

    gaps = [g for g in (report.get("data_quality") or {}).get("gaps") or [] if g.get("key") != "user_input"]
    grade = report.get("grade") or {}

    # be honest in the header about how much was restored: with nothing matched,
    # don't claim "your own names" -- the page is still pseudonymized
    if restored:
        opened = "Opened on your machine with your own names"
        restored_note = (f" · {_plural(unmatched, 'pseudonym')} not matched" if unmatched else "")
    elif unmatched:
        opened = "Opened on your machine · no names restored, pseudonyms shown"
        restored_note = ""
    else:
        opened = "Opened on your machine"
        restored_note = ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  :root {{ --ink:#1d1f2e; --ink-2:#585c70; --rule:#d9dbe6; --carbon:#3a3f9e; --pale:#e3e5f6;
          --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace; }}
  body {{ margin:0; background:#fff; color:var(--ink); font:16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
  main {{ max-width:860px; margin:0 auto; padding:32px 16px 48px; }}
  h1 {{ font-size:24px; margin:0; }}
  .meta, .scope, .note, .muted, .foot {{ color:var(--ink-2); }}
  .meta {{ font-size:14px; }}
  .totals {{ font-size:20px; font-weight:600; margin:20px 0 4px; }}
  h2 {{ font-size:17px; margin:28px 0 6px; }}
  .lead {{ font-size:19px; margin:0 0 12px; }}
  .step {{ font-family:var(--mono); font-size:.92em; overflow-wrap:anywhere; }}
  table {{ width:100%; border-collapse:collapse; }}
  th {{ text-align:left; font-size:13px; color:var(--ink-2); font-weight:600; padding:6px 8px 6px 0; border-bottom:2px solid var(--ink); }}
  td {{ padding:12px 8px 12px 0; border-bottom:1px solid var(--rule); vertical-align:top; font-size:15px; }}
  .num {{ text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }}
  .note {{ display:block; font-size:13px; }}
  .find + .find {{ margin-top:6px; }}
  td.barcell {{ width:28%; padding-top:17px; }}
  .spendbar {{ position:relative; height:10px; background:var(--pale); border-radius:2px; overflow:hidden; }}
  .spendbar i {{ position:absolute; left:0; top:0; bottom:0; background:var(--carbon); }}
  .spendbar i.rep {{ background:repeating-linear-gradient(135deg, var(--carbon) 0 3px, #9da1dd 3px 6px); }}
  .spendbar, .spendbar i {{ -webkit-print-color-adjust:exact; print-color-adjust:exact; }}
  tr.has-try td {{ border-bottom:0; padding-bottom:2px; }}
  tr.try td {{ padding-top:0; font-size:14.5px; }}
  tr.free {{ color:var(--ink-2); }}
  .foot {{ font-size:14px; margin-top:20px; }}
  dt {{ font-weight:600; margin-top:10px; }} dd {{ margin:0; color:var(--ink-2); }}
  @media (max-width:700px) {{ td.barcell, th.barh {{ display:none; }} }}
  @media print {{ main {{ padding:0; max-width:none; }} tr {{ break-inside:avoid; }} tr.has-try {{ break-after:avoid; }} }}
</style></head>
<body><main>
  <h1>{html.escape(title)}</h1>
  <div class="meta">{f"Dataset grade {html.escape(str(grade.get('grade')))} ({html.escape(str(grade.get('word', '')).lower())}) · " if grade.get("grade") else ""}{opened}{restored_note}</div>
  <p class="totals">{_money(cost)} across {_plural(t.get("runs") or 0, "run")}, {_plural(t.get("llm_calls") or 0, "LLM call")} and {_plural(t.get("tool_calls") or 0, "tool call")}, {t.get("tokens") or 0:,} tokens.</p>
  {scope}
  <h2>Where the money went</h2>
  <p class="lead">{" ".join(lead)}</p>
  <table>
    <thead><tr><th>Step</th><th class="barh"><span hidden>Share of spend</span></th><th class="num">Cost</th><th>Repeats</th></tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table>
  <p class="foot">Repeats are calls with the same input and the same result as an earlier call.</p>
  {("<h2>Not in your traces</h2><dl>" + "".join(f"<dt>{_text(g.get('label', '')).capitalize()}</dt><dd>{_text(g.get('effect', ''))}</dd>" for g in gaps) + "</dl>") if gaps else ""}
  <p class="foot">{_text(summary.get("notes") or "")}</p>
</main></body></html>
"""
