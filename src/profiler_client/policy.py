"""The words a prepared dataset may contain in clear text. Everything else
-- project names, script names, MCP servers, custom tools, commands,
paths, outputs -- is written as a pseudonym.

These lists are reviewed public vocabulary: names of widely used
programs and Claude Code's built-in tools, never anything a user or
company chose. Adding a word is a privacy decision: change
``POLICY_VERSION`` with it and say why in the commit.
"""
from __future__ import annotations

# 2: Codex CLI's built-in tools, ``gpt-``/``o``-series model ids and OpenAI
#    finish reasons, for Codex sessions and OpenAI chat trajectories.
POLICY_VERSION = "2"

# Claude Code's built-in tools. Any other tool name (MCP, custom) is a pseudonym.
BUILTIN_TOOLS = frozenset({
    "Agent", "AskUserQuestion", "Bash", "BashOutput", "Edit", "ExitPlanMode", "Glob", "Grep", "KillShell", "LS",
    "MultiEdit", "NotebookEdit", "NotebookRead", "Read", "Skill", "SlashCommand", "Task", "TodoWrite", "ToolSearch",
    "WebFetch", "WebSearch", "Write",
})

# Codex CLI's built-in tools (function and custom tool calls, plus the
# web and tool search items it records).
CODEX_TOOLS = frozenset({
    "apply_patch", "exec", "exec_command", "local_shell", "request_user_input", "request_user_input_async", "shell",
    "tool_search", "update_plan", "view_image", "wait", "web_search", "write_stdin",
})

TOOLS = BUILTIN_TOOLS | CODEX_TOOLS

# Programs whose first sub-command is what identifies the operation (the
# same set the profiler server splits by), each with the sub-commands that
# may appear in clear text. Any other sub-command -- a ``make`` target, a
# script name -- is a pseudonym, so operations stay apart without showing.
SUBCOMMAND_PROGRAMS: dict[str, frozenset[str]] = {
    "git": frozenset({
        "add", "blame", "branch", "checkout", "cherry-pick", "clone", "commit", "config", "diff", "fetch", "grep",
        "init", "log", "merge", "pull", "push", "rebase", "remote", "reset", "restore", "rev-parse", "show", "stash",
        "status", "switch", "tag", "worktree",
    }),
    "gh": frozenset({"api", "auth", "issue", "pr", "release", "repo", "run", "workflow"}),
    "npm": frozenset({"add", "ci", "exec", "install", "run", "start", "test"}),
    "pnpm": frozenset({"add", "exec", "install", "run", "start", "test"}),
    "yarn": frozenset({"add", "install", "run", "start", "test"}),
    "docker": frozenset({"build", "compose", "exec", "images", "logs", "ps", "pull", "push", "rm", "run", "stop"}),
    "kubectl": frozenset({"apply", "delete", "describe", "exec", "get", "logs", "rollout"}),
    "uv": frozenset({"add", "lock", "pip", "run", "sync", "venv"}),
    "pip": frozenset({"freeze", "install", "list", "show", "uninstall"}),
    "cargo": frozenset({"build", "check", "clippy", "fmt", "run", "test"}),
    "go": frozenset({"build", "fmt", "mod", "run", "test", "vet"}),
    "railway": frozenset({"link", "logs", "run", "status", "up", "variables"}),
    "make": frozenset(),
}

# Programs named alone, whatever their arguments.
PLAIN_PROGRAMS = frozenset({
    "awk", "cat", "chmod", "cp", "curl", "diff", "echo", "env", "find", "grep", "head", "jq", "kill", "ls", "mkdir",
    "mv", "npx", "ps", "pwd", "pytest", "rg", "rm", "ruff", "sed", "sort", "tail", "tar", "touch", "wc", "wget",
    "which",
})

# Script runners: ``python x.py`` is written ``python <pseudonym>``.
RUNNERS = frozenset({"bash", "node", "perl", "python", "ruby", "sh", "zsh"})

# Modules ``python -m`` may name in clear text.
PYTHON_MODULES = frozenset({"black", "build", "http.server", "json.tool", "mypy", "pip", "pytest", "ruff", "unittest",
                            "venv"})

# File types a path may be reduced to; any other suffix is a pseudonym.
FILE_SUFFIXES = frozenset({
    ".cfg", ".css", ".csv", ".go", ".html", ".ini", ".ipynb", ".java", ".js", ".json", ".jsonl", ".jsx", ".md",
    ".py", ".rs", ".sh", ".sql", ".toml", ".ts", ".tsx", ".txt", ".yaml", ".yml",
})

# Model ids are kept only when they start with one of these and hold only
# ``[a-z0-9.-]``; fine-tuned or custom model names (``ft:...``) often carry
# a customer or project name.
MODEL_PREFIXES = ("claude-", "gpt-", "o1", "o3", "o4")

# Why an LLM response stopped (Anthropic API ``stop_reason``
# and OpenAI ``finish_reason``).
STOP_REASONS = frozenset({"end_turn", "max_tokens", "pause_turn", "refusal", "stop_sequence", "tool_use",
                          "content_filter", "length", "stop", "tool_calls"})

# Fixed words the converter itself writes.
STRUCTURE_WORDS = frozenset({"llm_call", "←", "user_prompt", "respond", "(error)", "inline", "-m", "other",
                             "unknown", "mcp"})

AGENTS = frozenset({"claude-code", "claude-code-subagent", "codex"})
# Workflow prefixes: ``<source>/<pseudonym or kept name>``.
SOURCES = frozenset({"claude-code", "codex"})
EVENT_TYPES = frozenset({"decision", "tool_call"})

VOCABULARY = frozenset(
    TOOLS | set(SUBCOMMAND_PROGRAMS) | {s for subs in SUBCOMMAND_PROGRAMS.values() for s in subs}
    | PLAIN_PROGRAMS | RUNNERS | PYTHON_MODULES
    | FILE_SUFFIXES | STRUCTURE_WORDS
)
