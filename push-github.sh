#!/usr/bin/env bash
# Push a clean release commit to GitHub.
# Creates an orphan commit from the current working tree, excluding
# GitLab-specific files, then force-pushes to the github remote.
#
# Usage: ./push-github.sh ["Release message"]

set -e

MESSAGE="${1:-Release}"

echo "Building GitHub release: $MESSAGE"

git checkout --orphan github-release
git add -A
git rm --cached .gitlab-ci.yml

GIT_AUTHOR_NAME="CUCM Tools" \
GIT_AUTHOR_EMAIL="noreply@github.com" \
GIT_COMMITTER_NAME="CUCM Tools" \
GIT_COMMITTER_EMAIL="noreply@github.com" \
git commit -m "$MESSAGE"

git push github github-release:main --force

git checkout main
git branch -D github-release

echo "Done."
