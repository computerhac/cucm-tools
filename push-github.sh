#!/usr/bin/env bash
# Push a clean release commit to GitHub.
# Wizard mode: shows recent commits, prompts for a release message,
# cleans up any stale github-release branch, then force-pushes.

set -e

echo "========================================"
echo "  CUCM Tools — GitHub Release Wizard"
echo "========================================"
echo ""

# Show recent commits for reference
echo "Recent commits on main:"
git log main --oneline -10
echo ""

# Prompt for release message
read -rp "Release message: " MESSAGE
if [[ -z "$MESSAGE" ]]; then
  echo "Aborted — no message entered."
  exit 1
fi
echo ""

# Confirm
echo "Will push to GitHub with message:"
echo "  \"$MESSAGE\""
echo ""
read -rp "Proceed? [y/N] " CONFIRM
if [[ "$CONFIRM" != "y" && "$CONFIRM" != "Y" ]]; then
  echo "Aborted."
  exit 0
fi
echo ""

# Clean up any stale github-release branch/worktree from a previous failed run
if git worktree list | grep -q 'github-release'; then
  echo "Cleaning up stale worktree..."
  git worktree remove --force github-release 2>/dev/null || true
fi
if git show-ref --verify --quiet refs/heads/github-release; then
  echo "Removing stale github-release branch..."
  git branch -D github-release
fi
# Remove any leftover untracked .gitlab-ci.yml before switching branches
rm -f .gitlab-ci.yml

echo "Building release commit..."
git checkout --orphan github-release
git add -A
git rm --cached .gitlab-ci.yml 2>/dev/null || true

GIT_AUTHOR_NAME="CUCM Tools" \
GIT_AUTHOR_EMAIL="noreply@github.com" \
GIT_COMMITTER_NAME="CUCM Tools" \
GIT_COMMITTER_EMAIL="noreply@github.com" \
git commit -m "$MESSAGE"

echo "Pushing to GitHub..."
git push github github-release:main --force

# Clean up — remove .gitlab-ci.yml before switching back so git doesn't complain
rm -f .gitlab-ci.yml
git checkout main
git branch -D github-release

echo ""
echo "Done. Released: \"$MESSAGE\""
