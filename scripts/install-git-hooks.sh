#!/bin/sh
set -eu

repo_root=$(git rev-parse --show-toplevel)
configured_hooks=$(git config --get core.hooksPath || true)
if [ -n "$configured_hooks" ]; then
    printf '%s\n' "Existing core.hooksPath ($configured_hooks) is configured; integrate .githooks/pre-commit there instead." >&2
    exit 1
fi

# The common hooks directory also protects this repository's linked worktrees.
common_git_dir=$(git rev-parse --path-format=absolute --git-common-dir)
source_hook="$repo_root/.githooks/pre-commit"
target_hook="$common_git_dir/hooks/pre-commit"

if [ -e "$target_hook" ] || [ -L "$target_hook" ]; then
    if cmp -s "$source_hook" "$target_hook"; then
        printf '%s\n' 'Gitleaks pre-commit hook is already installed.'
        exit 0
    fi
    printf '%s\n' 'An existing pre-commit hook was preserved; integrate .githooks/pre-commit manually.' >&2
    exit 1
fi

mkdir -p "$common_git_dir/hooks"
install -m 0755 "$source_hook" "$target_hook"
printf '%s\n' 'Installed Gitleaks pre-commit hook for this repository and its linked worktrees.'
