# What the client can send

A short list for the privacy notice. It covers profiler-client v0.36.0
(contract 2, policy 5). The code that decides it is
[`policy.py`](../src/profiler_client/policy.py) for the clear-text words and
[`contract.py`](../src/profiler_client/contract.py) for the event fields. The
[README](../README.md#what-leaves-your-machine) explains how the pseudonyms work.

## Only one command sends anything

`profiler-client app` is the only command that opens a network connection. The
others work on this machine only: `prepare`, `check`, `validate`, `inspect`,
`open`, `show-event`, `forget` and `version`. `prepare` writes a dataset
directory, and nothing leaves until you send it, either with `app` or by
uploading `events.jsonl` on the site yourself.

## What a dataset holds: `events.jsonl`

This file is the only data that is ever uploaded. Its first line is a seal:
the client, contract and policy versions; what `parent` means; the number of
events; the kept names; the SHA-256 of the events; and a `key_id` that names
the key without revealing it. Then there is one line per LLM call or tool call.

| Option | What is sent | What never leaves the machine |
| --- | --- | --- |
| **Default** (`prepare`, no options; the app with both boxes off) | Each call's **numbers**: token counts, latency, cost.<br>Its **kind** (`decision` or `tool_call`) and outcome (`success`, `error` or a stop reason).<br>**Clear text from the public vocabulary only**: built-in tool names, well-known programs and sub-commands, file types, `claude-*`/`gpt-*`/`o`-series model ids.<br>**Pseudonyms** (`h:` + HMAC-SHA256) of everything else: workflow, agent, step and tool names; session, run, trace, span and call ids; every tool input and output; your own event log's field names and values.<br>The **order** of calls and which call started which. | Prompts and model replies.<br>Your request text.<br>Tool output text, file contents, paths and working directories, commands' arguments.<br>Timestamps (durations are kept).<br>The key, unless you give it to someone. |
| **`--keep-names`** ("Send workflow, agent, step and tool names as they are") | Everything in the default, but workflow, agent, step and tool names, and your event log's field names, are in **clear text**. This applies only to single words of up to 80 characters (letters, digits, `_ . / -`); a longer name stays a pseudonym. Every kept name is listed in the seal. | Values: tool inputs and outputs, and your event log's field values, are still pseudonyms. Prompts, replies and text, as in the default. |
| **`--llm-fingerprint`** ("Include a fingerprint of what the model read (no text is sent)"; old name `--llm-inputs`) | Everything in the default, plus **one pseudonym per LLM call** of everything the model had read so far in its conversation. It only says whether two calls read exactly the same thing. Claude Code, Codex and chat trajectories only. | The prompt, the conversation and the reply. The pseudonym can't be turned back into text without the key, and even with the key only a guessed text can be checked. |
| **A kept key** (`--key-file FILE`, `--key-env VAR` or `--key-command COMMAND`) | The same fields as the default. Because the key is the same every time, the same value gets the same pseudonym across datasets, so datasets can be compared over time. The seal's `key_id` shows which datasets share a key. | The key itself: it is never written to the dataset, the manifest or the seal. [How the key is protected](#how-the-key-is-protected) says what keeps it on this machine. Without one of these options, a fresh key is made for each `prepare` and then thrown away. |

**This is pseudonymization, not anonymity.** Counts, the order of steps,
timings and costs are visible. Equal pseudonyms show that two values were
equal.

## What the app sends besides the dataset

`profiler-client app` sends to the Openly7 server:

- the `events.jsonl` above;
- the client token (`pc_…`) you paste in, as the `Authorization` header. It is held in the app's memory and never saved;
- the analysis settings: the kind of report and, if you give them, the two token prices;
- a random `Idempotency-Key` per request, so a retried request doesn't run twice.

It downloads the report. The report holds pseudonyms only; the app restores
your names on this machine.

It never sends:

- the trace files you choose (they are copied into a temporary folder on this machine and deleted when the app exits; an interrupted app, for example one killed, or on Windows ended by a sign-out or shutdown, can leave the folder behind, and deleting it at the next start is planned, [#58](https://github.com/ipintotech-lab/profiler-client/issues/58));
- `manifest.json`;
- the name list;
- the event references.

## Kept on this machine only

| File | Where | What it holds |
| --- | --- | --- |
| name list | `~/.local/share/profiler-client/names/` | pseudonym → original name, so reports show your names |
| event references | `~/.local/share/profiler-client/refs/` | which trace file, record and place each event came from, for `show-event` |
| `manifest.json` | the dataset directory | a summary of the dataset (counts, versions, options used) |
| name map | where `names` writes it (by default beside the report JSON) | pseudonym → name for that report's datasets, loaded into your browser by the Openly7 page; never uploaded |
| report page | where `open` writes it (by default beside the report JSON) | the report with your names restored |
| key file | where you put it, if you use `--key-file` | the secret key, protected as [below](#how-the-key-is-protected) |

On Mac and Linux, the name list, the event references and the report page
are readable only by you (mode 0600). **On Windows
they are not protected by the client.** Windows has no such file modes, so
each file gets the access of the folder it is in. For the name list and the
event references, that is your user profile folder, which by default you, the
machine's administrators and Windows itself can read. The report page gets the
access of the folder you write it to, which may be a shared folder. Protecting
these files on Windows as the key is protected is planned
([#56](https://github.com/ipintotech-lab/profiler-client/issues/56)).

`--no-name-list` skips the name list and the event references.
`profiler-client forget` deletes them.

## How the key is protected

Whoever holds the key can check a guessed value against a pseudonym, so the
key is what the pseudonyms' protection rests on. The client never sends it
anywhere.

| Where the key comes from | What protects it | What it does not protect against |
| --- | --- | --- |
| No key option (the default) | A fresh key is made for each `prepare`, held in memory and never written. | Nothing is kept once `prepare` ends. |
| `--key-file` on Mac and Linux | The file holds the key in hex and must be readable only by you (mode 0600). `prepare` refuses a key file that other users can read. | An administrator of the machine (root), malware running as you, and copies of the file, such as backups. |
| `--key-file` on Windows | The key is encrypted with Windows DPAPI for your Windows account. A copy of the file can't be decrypted by another account or on another machine. A key file that an earlier version wrote without encryption is encrypted the first time it is used. | An administrator of the machine or of your Windows domain, malware running as you, and anyone who signs in as you. Copies of a key file an earlier version wrote without encryption, such as in OneDrive, backups or File History, stay unencrypted: delete them. |
| `--key-env VAR` | Whatever protects that environment variable: your shell, your CI system or your secret store. | Anyone who can read your processes' environment, such as an administrator or malware running as you. |
| `--key-command COMMAND` | The secret store the command reads from. The client runs the command without a shell, reads the key from its output and never shows that output. The command's own error output is shown on the terminal unchanged, so the command must not print the key there. | Whoever can read that secret store or run the command as you. |

Only one key option can be given. Whatever the source, the key is never
written to the dataset, the manifest, the seal or the name list, and no
message the client prints shows it; only its `key_id` appears. A key that
can't be read (another Windows account or machine, a damaged file, an empty
variable, a failing or slow command) stops `prepare` with an error. No new key is
made in its place, because a dataset made with a new key can't be compared
with earlier ones.

The Openly7 server also sees what any web server sees, such as your IP
address and the time of each request. Its privacy notice covers that and how
long data is kept; this client does not control it.
