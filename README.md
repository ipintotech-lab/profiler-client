# profiler-client

Prepare your AI agent traces for the AI Agent Profiler on your own machine.
`profiler-client` turns your agent traces into a dataset that keeps what the profiler measures (which operations ran, how often the same
input repeated, tokens, latency, cost) and drops or pseudonymizes everything
that identifies you, your projects or your data.

It never opens a network connection. You look at the result before anything
is sent.

## Install

```
pip install -e .
```

Python 3.10+; the only dependency is pydantic.

## Use

```
profiler-client prepare ~/.claude/projects/<project>/<session>.jsonl -o analysis-ready
profiler-client prepare ~/.codex/sessions/2026/10/18/rollout-*.jsonl -o analysis-ready
profiler-client prepare spans.json --keep-names -o analysis-ready
profiler-client inspect analysis-ready
profiler-client validate analysis-ready
profiler-client version
```

`prepare` reads only the files you name: directories and symlinks are
refused, and it prints the list before reading. It writes `events.jsonl` and
`manifest.json` into a new or empty directory.

## Supported traces

Each file is JSON Lines or one JSON document, and its format is detected:

| Format | What it is |
| --- | --- |
| `claude-code` | Claude Code sessions, `~/.claude/projects/<project>/<session>.jsonl` |
| `codex` | OpenAI Codex CLI sessions, `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` |
| `otel` | OpenTelemetry OTLP JSON span exports with GenAI (`gen_ai.*`) or OpenInference attributes: LangChain, LlamaIndex, OpenLLMetry, Langfuse, Phoenix and most agent frameworks |
| `openai-chat` | OpenAI-style chat trajectories, `{"messages": [{"role", "content", "tool_calls"}, ...]}` |
| `events` | your own agent's event log in the profiler's event shape (`trace_id`, `run_id`, `workflow`, `event_type`, `node`, `tokens`, `cost`, ...), with the field aliases the profiler accepts |

## Your own step names

In your own agents' traces, the workflow, agent, node and tool names are
words you chose, so by default they are pseudonyms too, and the report shows
`h:…`. With `--keep-names` they stay readable, so findings are easier to act
on. Only single-word names (`fetch_diff`, `billing/nightly`) are kept; a
sentence or anything with spaces stays a pseudonym. Inputs, outputs, ids,
commands and script names are never kept. Every kept name is listed under
`kept_names` in the manifest: check it with `inspect` before you send the
dataset. The lint accepts no other names.

## What leaves your machine

| Kept in clear text | Pseudonymized | Dropped |
| --- | --- | --- |
| Claude Code's and Codex's built-in tool names | project directory | prompts and replies |
| well-known programs and sub-commands (`git status`) | session, prompt, message, trace and span ids | tool output text |
| file types (`.py`) | MCP and custom tool names, your workflow and agent names (unless `--keep-names`) | your request text |
| `claude-*`, `gpt-*` and `o`-series model ids, Claude Code and Codex versions | script names, other programs and sub-commands | timestamps (durations are kept) |
| token counts, latency, cost, stop reasons | every tool input and every tool output | working directories |

The full clear-text vocabulary is in
[`src/profiler_client/policy.py`](src/profiler_client/policy.py). Any word not
on it becomes a pseudonym.

A pseudonym is `h:` plus HMAC-SHA256 of the value under a secret key. Equal
values get equal pseudonyms, which is all the profiler needs to tell whether
an operation gave the same output for the same input. Without the key nobody
can check a guess. By default every `prepare` uses a fresh key that isn't kept,
so two datasets can't be linked. To compare datasets over time, pass
`--key-file ~/.config/profiler-client/key`; the file is created with mode 0600.
If you lose it, you can no longer compare.

Before writing anything, `prepare` runs a privacy lint over every event. Every
string must be a pseudonym, built from policy words or a kept name listed in
the manifest, otherwise nothing is written and the error names the field, never the value. `validate` runs it
again, and also checks that `events.jsonl` still matches the hash in the
manifest.

**This is pseudonymization, not anonymity.** How often things happen, the
order of steps and timings stay visible, and they can say something about how
you work.

## Development

```
pip install -e '.[dev]'
pytest
```

The tests feed canary strings through every converter (raw, base64,
URL-encoded, hex and JSON-escaped) and fail if any of them shows up in the
output. They also run every command with sockets disabled.

## Terms

The full terms and privacy notes are at
[agentsprofiler-production.up.railway.app/client-terms](https://agentsprofiler-production.up.railway.app/client-terms).
In short:

- **The software.** The client is open source under Apache-2.0. You may use,
  change and share it under that license. It is provided as is, without
  warranties, and its authors are not liable for what happens when you use
  it (sections 7 and 8 of the license).
- **Your traces.** Run it only on traces you are allowed to use. If they
  belong to your employer or your customers, get their permission first.
- **Your dataset.** The client runs on your machine and sends nothing. The
  dataset it writes is yours, and you decide whether to send it anywhere.
- **Not anonymous.** The dataset is pseudonymized, not anonymous: counts and
  timings can still say something about your work. Read it with `inspect`
  before you share it.
- **Sending it to the beta.** Uploading a dataset to the hosted AI Agent
  Profiler beta is covered by the
  [beta terms and privacy notice](https://agentsprofiler-production.up.railway.app/#terms),
  which you accept before your first upload.

## License

Apache-2.0. See [LICENSE](LICENSE).
