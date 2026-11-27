#!/usr/bin/env bash
# The factory on your own machine, on your Claude or ChatGPT subscription
# (no API key). Rules: AGENTS.md. Nothing is pushed or posted until you say y.
#
#   scripts/factory.sh build  <issue> claude|codex
#       The agent works on the issue in its own worktree next to this repo
#       and only edits files. Then you see the change and the PR text, and
#       you decide whether to commit, push and open the PR.
#   scripts/factory.sh review <pr> claude|codex
#       The agent reviews the PR by "Code Review Rules" in AGENTS.md. Then
#       you decide whether to post the review as a PR comment.
#
# Needs: gh (signed in), git, jq, and `claude` or `codex` (signed in).
set -euo pipefail

# this repo's set-up, as in .github/workflows/test.yml
setup_venv() {
  python3 -m venv .venv
  .venv/bin/pip install -q -e '.[dev]'
}

die() { echo "factory: $*" >&2; exit 1; }
ask() { local a; read -r -p "$1 [y/N] " a; [[ $a == y || $a == Y ]]; }

[[ $# == 3 ]] || die "usage: $0 build <issue> claude|codex | review <pr> claude|codex"
cmd=$1 num=$2 agent=$3
[[ $cmd == build || $cmd == review ]] || die "unknown command: $cmd"
[[ $num =~ ^[0-9]+$ ]] || die "not a number: $num"
case $agent in
  claude) coauthor="Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
          footer="🤖 Generated with [Claude Code](https://claude.com/claude-code)" ;;
  codex)  coauthor="Co-Authored-By: Codex <noreply@openai.com>"
          footer="🤖 Generated with Codex" ;;
  *) die "agent must be claude or codex" ;;
esac
command -v "$agent" >/dev/null || die "$agent is not installed"

root=$(git rev-parse --show-toplevel)
repo=$(gh repo view --json nameWithOwner -q .nameWithOwner)
out=$(mktemp -d)

# Only text written by people with write access reaches the agent.
check_author() {
  gh api "repos/$repo/collaborators/$1" --silent 2>/dev/null \
    || die "$1 is not a collaborator: copy the request into an issue of your own"
}

# run_agent <dir> <prompt> <read-only: 0|1>: the agent's last message goes to $out/last.md
run_agent() {
  local dir=$1 prompt=$2 ro=$3
  echo "factory: $agent is working in $dir (this can take a while)"
  if [[ $agent == claude ]]; then
    local deny=()
    [[ $ro == 1 ]] && deny=(--disallowedTools "Edit,Write,NotebookEdit")
    (cd "$dir" && claude -p "$prompt" --permission-mode auto "${deny[@]}") > "$out/last.md"
  elif [[ $ro == 1 ]]; then
    (cd "$dir" && codex exec review --base origin/main -o "$out/last.md" "$prompt")
  else
    codex exec -C "$dir" -s workspace-write -o "$out/last.md" "$prompt"
  fi
}

build() {
  local issue title author branch wt
  issue=$(gh issue view "$num" --repo "$repo" --json title,body,author,state)
  [[ $(jq -r .state <<<"$issue") == OPEN ]] || die "issue #$num is not open"
  title=$(jq -r .title <<<"$issue")
  author=$(jq -r .author.login <<<"$issue")
  check_author "$author"
  branch="factory/$num-$agent"
  wt="$(dirname "$root")/$(basename "$root")-factory-$num-$agent"
  [[ -e $wt ]] && die "$wt exists: finish or remove it first (git worktree remove $wt)"

  git -C "$root" fetch -q origin main
  git -C "$root" worktree add -q -b "$branch" "$wt" origin/main
  echo "factory: setting up $wt"
  (cd "$wt" && setup_venv)

  run_agent "$wt" "You are working on GitHub issue #$num of $repo, in this worktree (branch $branch).

Title: $title

$(jq -r .body <<<"$issue")

Follow AGENTS.md, section by section: check the issue first, build only what it asks,
run the tests with every change (the venv is .venv), and fix failures at most 3 rounds.
Only edit files: do not commit, push, open a PR or call GitHub; the owner does that next.
If you stop because the issue is unclear or breaks a rule, say why as your final message.
Otherwise your final message is the PR description itself, in the format AGENTS.md gives,
with 'Closes #$num'. Write nothing else in that message." 0

  echo; echo "===== changes ====="; git -C "$wt" status --short; git -C "$wt" diff --stat
  echo; echo "===== $agent's final message (the PR text) ====="; cat "$out/last.md"; echo
  if [[ -z $(git -C "$wt" status --porcelain) ]]; then
    echo "factory: no changes. Worktree: $wt"; return
  fi
  ask "Commit, push $branch and open the PR?" || { echo "factory: left as is in $wt"; return; }
  git -C "$wt" add -A
  git -C "$wt" commit -q -m "$title (#$num)" -m "$coauthor"
  git -C "$wt" push -q -u origin "$branch"
  grep -q "Closes #$num" "$out/last.md" || printf '\nCloses #%s\n' "$num" >> "$out/last.md"
  grep -qF "$footer" "$out/last.md" || printf '\n%s\n' "$footer" >> "$out/last.md"
  gh pr create --repo "$repo" --base main --head "$branch" --title "$title" --body-file "$out/last.md"
  echo "factory: worktree kept for fixes: $wt (remove with: git worktree remove $wt)"
}

review() {
  local pr author wt
  pr=$(gh pr view "$num" --repo "$repo" --json author,state,isCrossRepository)
  [[ $(jq -r .state <<<"$pr") == OPEN ]] || die "PR #$num is not open"
  [[ $(jq -r .isCrossRepository <<<"$pr") == false ]] || die "PR #$num comes from a fork"
  author=$(jq -r .author.login <<<"$pr")
  [[ $author == app/* ]] || check_author "$author"
  wt="$(dirname "$root")/$(basename "$root")-review-$num"
  [[ -e $wt ]] && die "$wt exists: remove it first (git worktree remove --force $wt)"

  git -C "$root" fetch -q origin main "+refs/pull/$num/head:refs/remotes/origin/pr/$num"
  git -C "$root" worktree add -q --detach "$wt" "origin/pr/$num"
  echo "factory: setting up $wt"
  (cd "$wt" && setup_venv)

  local prompt="Review pull request #$num of $repo (checked out here; origin/main is fetched)
by the 'Code Review Rules' section of AGENTS.md. Read the issue it closes. Change no file.
Your final message is the review itself, blocking findings first."
  run_agent "$wt" "$prompt" 1

  echo; echo "===== $agent's review ====="; cat "$out/last.md"; echo
  if ask "Post it as a comment on PR #$num?"; then
    { echo "**Review by $agent** (local, \`scripts/factory.sh\`)"; echo; cat "$out/last.md"; } > "$out/comment.md"
    gh pr comment "$num" --repo "$repo" --body-file "$out/comment.md"
  fi
  git -C "$root" worktree remove --force "$wt"
}

"$cmd"
