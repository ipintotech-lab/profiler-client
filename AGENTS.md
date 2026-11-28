# Working rules

These rules are for every coding agent in this repo. Codex reads this file;
Claude Code reads it through `CLAUDE.md`.

## Event format changes

- Release order: when the event format changes (`profiler_client/contract.py`,
  the server's `TraceEvent`, or the seal), the server that accepts the change
  deploys first. Only once it is live is a client release that emits it tagged.
  The server validates uploads strictly (unknown fields are refused), so a
  client that emits a field the live server doesn't know gets its uploads
  refused.
- Propose any event format change to the project owner and wait for their OK
  before building it.

## The factory

Work comes in as a GitHub issue (template
`.github/ISSUE_TEMPLATE/factory-task.yml`) written by someone with write
access. An agent builds it on a branch, CI and the reviewers check the pull
request, the owner merges, and the owner tags the release. The agent can run
on GitHub (`.github/workflows/claude-build.yml`, label `factory`) or on the
owner's machine (`scripts/factory.sh`); the rules are the same.

This repo is public: anyone can read the issues, PRs, comments and workflow
logs. Never put server code, secrets, real traces or personal data in them.

### Building

- One issue, one branch, one PR. Keep it small. Do only what the issue asks.
- If the issue is unclear or conflicts with these rules, stop and say so (on
  the issue, or in your final message). Don't guess.
- Never touch secrets, tokens or key files, the sign-in and sending code, or
  release tagging, unless the issue says so explicitly. Never push a `v*` tag:
  the owner tags releases.
- What leaves the user's machine is the product's promise
  (`docs/what-is-sent.md`): prompts, replies and code are never sent; names are
  pseudonymized with a key only the user holds unless they opt in. Any change
  to what is sent, or to that text, is a sensitive change and says so in the PR.
- Stack: Python 3.10 or later, pydantic only, the `profiler-client` command
  (`src/profiler_client/cli.py`); `app.py` is the local page that prepares and
  sends; the report renderer is `src/profiler_client/static/`.
- Set up and test, as CI does:
  `python -m venv .venv && .venv/bin/pip install -e '.[dev]'`, then
  `.venv/bin/ruff check src tests scripts`, `.venv/bin/python scripts/check_version.py`
  and `.venv/bin/python -m pytest -q`. CI also runs Windows and macOS.
- Run the tests with every change. If they fail, fix the cause and run them
  again, at most 3 rounds; then stop and report what still fails. Never skip,
  xfail, loosen or delete a test to get green.
- Commits end with the agent's own line: Claude Code
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`, Codex
  `Co-Authored-By: Codex <noreply@openai.com>`.
- The PR description: what and why, tests, what still needs the owner, then
  `Closes #<issue>`, and it ends with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`
  or `🤖 Generated with Codex`.

### Cross-repo contract

The server (`openly7/agents_profiler`, private) pins a tagged commit of
this client and serves its report renderer.

- A report change lands here first; the owner tags it; then a server PR pins
  the tag.
- Event format changes follow "Event format changes" above: proposed to the
  owner and OK'd before anything is built; the server goes live first.
- A PR that touches the report, the dataset or the event format says so at
  the top of its description and asks the owner to open the matching server
  issue (the server repo is private: don't describe its code here).

## Code Review Rules

For every reviewer: the review workflow, the Codex GitHub review, and
`scripts/factory.sh review`.

- You review a change you didn't write. Change nothing, push nothing, approve
  nothing, merge nothing: you only comment.
- Check: correctness; nothing new leaves the user's machine unless the issue
  says so, and no prompt, reply, code or real name is sent without the user's
  choice; no event format change (`contract.py`, the seal in `dataset.py`)
  without the owner's OK; no secrets or personal data; no network call where
  there was none; the tests prove what the issue asks and none was skipped or
  weakened; it still runs on Python 3.10 and on Windows.
- Report only real findings, each with file, line and a concrete failure.
  Blocking first. If there are none, say so in one line.
