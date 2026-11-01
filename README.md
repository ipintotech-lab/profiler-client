# profiler-client

Prepare your AI agent traces for the AI Agent Profiler on your own machine.
`profiler-client` turns Claude Code session transcripts into a dataset that
keeps what the profiler measures (which operations ran, how often the same
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
profiler-client inspect analysis-ready
profiler-client validate analysis-ready
profiler-client version
```

`prepare` reads only the files you name: directories and symlinks are
refused, and it prints the list before reading. It writes `events.jsonl` and
`manifest.json` into a new or empty directory.

## What leaves your machine

| Kept in clear text | Pseudonymized | Dropped |
| --- | --- | --- |
| Claude Code's built-in tool names | project directory | prompts and replies |
| well-known programs and sub-commands (`git status`) | session, prompt and message ids | tool output text |
| file types (`.py`) | MCP and custom tool names | your request text |
| `claude-*` model ids, Claude Code version | script names, other programs and sub-commands | timestamps (durations are kept) |
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
string must be a pseudonym or built from policy words, otherwise nothing is
written and the error names the field, never the value. `validate` runs it
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

The tests feed canary strings through the converter (raw, base64,
URL-encoded, hex and JSON-escaped) and fail if any of them shows up in the
output. They also run every command with sockets disabled.

## License

Apache-2.0. See [LICENSE](LICENSE).
