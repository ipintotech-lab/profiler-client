import base64
import json
import urllib.parse

import pytest

# Strings that must never appear in a prepared dataset, in any form.
CANARIES = [
    "acme-secret-project",
    "deploy_acme_prod.py",
    "mcp__acme_internal__lookup",
    "ACME_TOKEN_9f8e7d",
    "/home/alice/acme-secret-project/src/billing.py",
    "customer list for Acme Corp",
    "acme-finetune-v3",
    "make deploy-acme",
    "corp_cli",
]


def canary_forms(canary: str) -> list[str]:
    raw = canary.encode()
    return [
        canary,
        canary.lower(),
        base64.b64encode(raw).decode(),
        base64.urlsafe_b64encode(raw).decode(),
        urllib.parse.quote(canary),
        raw.hex(),
        json.dumps(canary)[1:-1],
    ]


def _assistant(session, msg_id, ts, content, model="claude-opus-4-1", cwd="/home/alice/acme-secret-project",
               stop="tool_use", usage=None):
    return {
        "type": "assistant", "sessionId": session, "timestamp": ts, "cwd": cwd, "version": "2.1.0",
        "message": {"id": msg_id, "model": model, "stop_reason": stop, "content": content,
                    "usage": usage or {"input_tokens": 100, "output_tokens": 20}},
    }


def _user(session, ts, content, prompt_id=None):
    row = {"type": "user", "sessionId": session, "timestamp": ts, "message": {"content": content}}
    if prompt_id:
        row["promptId"] = prompt_id
    return row


def _tool_use(tool_id, name, tool_input):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


def _result(tool_id, output, error=False):
    return {"type": "tool_result", "tool_use_id": tool_id, "content": output, "is_error": error}


@pytest.fixture
def transcript_rows():
    s = "sess-acme-secret-project-1"
    return [
        _user(s, "2026-10-01T10:00:00Z", "customer list for Acme Corp, ACME_TOKEN_9f8e7d", prompt_id="p1"),
        _assistant(s, "m1", "2026-10-01T10:00:02Z", [
            {"type": "text", "text": "Looking at acme-secret-project"},
            _tool_use("t1", "Bash", {"command": "git status", "description": "acme status"}),
            _tool_use("t2", "Bash", {"command": "python deploy_acme_prod.py --env prod"}),
            _tool_use("t3", "mcp__acme_internal__lookup", {"customer": "Acme Corp"}),
            _tool_use("t4", "Read", {"file_path": "/home/alice/acme-secret-project/src/billing.py"}),
            _tool_use("t5", "Bash", {"command": "make deploy-acme"}),
            _tool_use("t6", "Bash", {"command": "corp_cli sync --token ACME_TOKEN_9f8e7d"}),
        ]),
        _user(s, "2026-10-01T10:00:05Z", [
            _result("t1", "On branch main"),
            _result("t2", "deployed acme-secret-project", error=True),
            _result("t3", "customer list for Acme Corp"),
            _result("t4", "ACME_TOKEN_9f8e7d = 1"),
            _result("t5", "ok"),
            _result("t6", "ok"),
        ]),
        _assistant(s, "m2", "2026-10-01T10:00:09Z", [
            _tool_use("t7", "Bash", {"command": "git status", "description": "again"}),
        ], model="acme-finetune-v3"),
        _user(s, "2026-10-01T10:00:10Z", [_result("t7", "On branch main")]),
        _assistant(s, "m3", "2026-10-01T10:00:12Z", [{"type": "text", "text": "Done for Acme Corp"}],
                   stop="end_turn"),
        {"type": "cost-state", "sessionId": s,
         "modelUsage": {"claude-opus-4-1": {"costUSD": 0.5}, "acme-finetune-v3": {"costUSD": 0.1}}},
    ]


@pytest.fixture
def transcript_file(tmp_path, transcript_rows):
    path = tmp_path / "session.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in transcript_rows), encoding="utf-8")
    return path
