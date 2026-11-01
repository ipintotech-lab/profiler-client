"""The words a prepared dataset may contain in clear text. Everything else
-- project names, script names, MCP servers, custom tools, commands,
paths, outputs -- is written as a pseudonym.

These lists are reviewed public vocabulary: names of widely used
programs and Claude Code's built-in tools, never anything a user or
company chose. Adding a word is a privacy decision: change
``POLICY_VERSION`` with it and say why in the commit.
"""
from __future__ import annotations

POLICY_VERSION = "1"

# Claude Code's built-in tools. Any other tool name (MCP, custom) is a pseudonym.
BUILTIN_TOOLS = frozenset({
    "Agent", "AskUserQuestion", "Bash", "BashOutput", "Edit", "ExitPlanMode", "Glob", "Grep", "KillShell", "LS",
    "MultiEdit", "NotebookEdit", "NotebookRead", "Read", "Skill", "SlashCommand", "Task", "TodoWrite", "ToolSearch",
    "WebFetch", "WebSearch", "Write",
})

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

# Model ids are kept only when they match this prefix; fine-tuned or
# custom model names often carry a customer or project name.
MODEL_PREFIX = "claude-"

# Why an LLM response stopped (Anthropic API ``stop_reason``).
STOP_REASONS = frozenset({"end_turn", "max_tokens", "pause_turn", "refusal", "stop_sequence", "tool_use"})

# Fixed words the converter itself writes.
STRUCTURE_WORDS = frozenset({"llm_call", "←", "user_prompt", "respond", "(error)", "inline", "-m", "other",
                             "unknown", "mcp"})

AGENTS = frozenset({"claude-code", "claude-code-subagent"})
EVENT_TYPES = frozenset({"decision", "tool_call"})

VOCABULARY = frozenset(
    BUILTIN_TOOLS | set(SUBCOMMAND_PROGRAMS) | {s for subs in SUBCOMMAND_PROGRAMS.values() for s in subs}
    | PLAIN_PROGRAMS | RUNNERS | PYTHON_MODULES
    | FILE_SUFFIXES | STRUCTURE_WORDS
)
