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
profiler-client app   # opens a page on this computer: choose files, prepare, send, see the report
profiler-client check ~/.claude/projects/<project>/*.jsonl   # what the analysis can find, before preparing
profiler-client prepare ~/.claude/projects/<project>/<session>.jsonl -o analysis-ready
profiler-client prepare ~/.codex/sessions/2026/10/18/rollout-*.jsonl -o analysis-ready
profiler-client prepare spans.json --keep-names -o analysis-ready
profiler-client inspect analysis-ready
profiler-client validate analysis-ready
profiler-client open report.json
profiler-client forget report.json   # delete the local name list for that dataset
profiler-client version
```

`app` opens a page in your browser that runs on this computer. On it you
choose your trace files, set the options (names sent as they are, and "Include a
fingerprint of what the model read (no text is sent)"; both off by default), check the summary, paste
a client token from the Openly7 site and send. The page prepares the files
exactly like `prepare`, uploads only the sealed `events.jsonl`, waits for the
report and shows it with your names restored, as `open` does. It listens on
127.0.0.1 only, on a random port, and the address it prints carries a
one-time token that the page swaps for a cookie; any other host, origin or
request without that cookie is refused. The token you paste is held in memory
only. Your files are copied to a temporary folder that is deleted after
preparing, and everything else is deleted when the app exits (Ctrl-C). An
app that is interrupted, for example killed, or on Windows ended by a sign-out
or shutdown, can leave the folder behind; deleting it at the next start is
planned ([#58](https://github.com/ipintotech-lab/profiler-client/issues/58)).
`--server` picks another Openly7 address, `--port` a fixed port and
`--no-browser` just prints the address.

`check` reads your raw traces in memory and prints the grade the server will
give them, which findings the analysis can make from them (and which it
can't, and why), and what to turn on or collect to get more. It writes
nothing, keeps no key or name list and opens no connection. Its output holds
only counts, shares and thresholds, so it is safe to send to support; add
`--names` to list workflow and file names. `--prices IN,OUT` ($ per 1M input
and output tokens) shows what cost estimated from tokens would allow,
`--json` prints one object, and `--min-grade B` exits 1 when a workflow
grades below B. `prepare` prints the same summary when it finishes.

`prepare` saves a pseudonym-to-name list for the dataset in a private local
directory (`$XDG_DATA_HOME/profiler-client/names/`, `~/.local/share/...` by
default), outside the dataset directory — so it is never uploaded. On Mac and
Linux it is mode 0600; on Windows it has only the access of your user folder
(see [`docs/what-is-sent.md`](docs/what-is-sent.md#kept-on-this-machine-only)).
It holds names, plus the values of your event log's input fields that take at
most 20 short values (a band, a status, a flag), so a report can say
`defect_band=<1%`. Outputs, ids, prompts and every other input are never
written. Use
`--no-name-list` to skip saving it, and `profiler-client forget <report.json>`
(or a dataset hash, or `--all`) to delete it.

After downloading a report JSON, `open` writes a local HTML copy with matching
names restored, owner-only on Mac and Linux (mode 0600; on Windows it has the
access of the folder it is written to); no report data is sent anywhere. It
restores only from the list for that exact dataset, says how many pseudonyms it
couldn't match, and — since the HTML holds your real values — keep it local
rather than re-uploading or sharing it.

`prepare` reads only the files you name: directories and symlinks are
refused, and it prints the list before reading. It writes `events.jsonl` and
`manifest.json` into a new or empty directory. `events.jsonl` is the file you
upload: the profiler accepts only a file prepared by this client.

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
| well-known programs and sub-commands (`git status`) | session, prompt, message, tool call, trace and span ids | tool output text |
| file types (`.py`) | MCP and custom tool names, your workflow and agent names (unless `--keep-names`) | your request text |
| `claude-*`, `gpt-*` and `o`-series model ids, Claude Code and Codex versions | script names, other programs and sub-commands | timestamps (durations are kept) |
| token counts, latency, cost, stop reasons | every tool input and every tool output | working directories |

The full clear-text vocabulary is in
[`src/profiler_client/policy.py`](src/profiler_client/policy.py). Any word not
on it becomes a pseudonym.
[`docs/what-is-sent.md`](docs/what-is-sent.md) lists, per option, what is
sent and what never leaves the machine.

A pseudonym is `h:` plus HMAC-SHA256 of the value under a secret key. Equal
values get equal pseudonyms, which is all the profiler needs to tell whether
an operation gave the same output for the same input. Your own event log's
input gets one pseudonym per field, so the profiler can tell which field
decides an answer; the field names are pseudonyms too unless you pass
`--keep-names`. This shows how many fields an input has and which calls share
a field's value, never the value itself. Other formats keep one pseudonym for
the whole input, so a custom tool's argument names never leave the machine. Without the key nobody
can check a guess. By default every `prepare` uses a fresh key that isn't kept,
so two datasets can't be linked. To compare datasets over time, pass
`--key-file ~/.config/profiler-client/key`. On Mac and Linux the file is
created with mode 0600; on Windows the key is encrypted for your Windows
account (DPAPI), so a copy is useless to another account or machine. A team
can share one key from its secret store instead, with `--key-env VAR` or
`--key-command COMMAND` (the key in hex, 64 characters, e.g. from
`openssl rand -hex 32`). The command's own error output is shown on the
terminal unchanged, so it must not print the key there. If you lose the key, you can
no longer compare; a key that can't be read is an error, never replaced by a
new one. [`docs/what-is-sent.md`](docs/what-is-sent.md#how-the-key-is-protected)
says what protects the key on each system and what it doesn't. The seal records a `key_id` that
names the key without revealing it: the server combines several datasets into
one report only when they share a key, and `open` restores names from each of
the datasets a combined report lists.

An LLM call's input is dropped by default, so the report can't tell whether
the model was asked the same thing twice. With `--llm-fingerprint`, "include a
fingerprint of what the model read (no text is sent)" (Claude Code, Codex and
chat trajectories; `--llm-inputs` is the old name), each LLM call also gets one pseudonym of
everything the model read in its conversation so far, with call ids and
signatures left out so a re-run of the same conversation matches. The text
itself never leaves your machine, and the manifest records `llm_inputs`.
Two calls match only if their whole conversation matched up to that point, so
this finds repeats across re-runs of the same task, not inside one growing
chat. Claude Code transcripts don't hold its system prompt.

Before writing anything, `prepare` runs a privacy lint over every event. Every
string must be a pseudonym, built from policy words or a kept name listed in
the manifest, otherwise nothing is written and the error names the field, never the value. `validate` runs it
again, and also checks that `events.jsonl` still matches the hash in the
manifest.

Each event has its own id (`event_id`, a pseudonym of the message, tool call
or span id) in the same id space as its `parent`, so the profiler can draw
which step started which. A tool call's parent is the LLM call that chose it,
or its enclosing span in OpenTelemetry, or the LLM call whose output names
the tool span's call id (`gen_ai.tool.call.id`; the ids themselves are never
written) (`parent_relation: invokes`). For your
own event log the client can't know what `parent` means, so it says
`unspecified`.

| Field | Values | Meaning |
| --- | --- | --- |
| `event_id` | pseudonym, or absent (contract v1) | this event's id, in the same id space as `parent` |
| `parent` | an `event_id`, or absent | the event that started this one |
| `parent_by` | `"order"`, or absent (since v0.30.0) | `"order"`: the trace records no parent for this event, so the client took the LLM call that ran just before it (an OpenTelemetry tool span under an agent or chain span the client skips). The map draws it dashed as "ran next", never as a choice. Absent: `parent` is recorded by the trace. The analysis reads `parent` the same either way. |

The first line of `events.jsonl` is a seal: the client, contract and policy
versions, what `parent` means, the event count, the kept names and the
SHA-256 of the events after it. Datasets from clients before v0.7.0
(contract v1, no `event_id`) are still accepted. The profiler server refuses a file without a matching seal, then runs this
same privacy lint and contract check on every event. The seal is a checksum,
not a signature, so the server's own lint is what decides.

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
[openly7.com/client-terms](https://openly7.com/client-terms).
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
  [beta terms and privacy notice](https://openly7.com/#terms),
  which you accept before your first upload.

## License

Apache-2.0. See [LICENSE](LICENSE).
