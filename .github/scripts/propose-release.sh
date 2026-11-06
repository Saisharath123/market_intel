#!/usr/bin/env bash
# Create a reviewable digest-only PR against the latest compatible main.
set -euo pipefail
stage=${1:?Use staging or production}
[[ "$stage" == staging || "$stage" == production ]]
[[ "$GITHUB_SHA" =~ ^[0-9a-f]{40}$ ]]
source_values=${SOURCE_VALUES:?Source image values are required}
target="deploy/chart/values-$stage.yaml"
branch="release/$stage-$GITHUB_RUN_ID"
if git ls-remote --exit-code --heads origin "$branch" > /dev/null; then
  printf 'Release branch already exists and was preserved: https://github.com/%s/compare/main...%s?expand=1\n' "$GITHUB_REPOSITORY" "$branch" >> "$GITHUB_STEP_SUMMARY"
  exit 0
fi
git fetch origin main
if [ "$stage" = staging ]; then
  # A slow old build must not replace a newer source release.
  git diff --quiet "$GITHUB_SHA" origin/main -- app.py market services 'requirements*.txt' Dockerfile .dockerignore compose.yaml sonar-project.properties .github
else
  # Promotion must use the staged image set the operator actually reviewed.
  test "$(git rev-parse "$GITHUB_SHA:deploy/chart/values-staging.yaml")" = "$(git rev-parse origin/main:deploy/chart/values-staging.yaml)"
fi
git switch --create "$branch" origin/main
python .github/scripts/gitops_release.py update --source "$source_values" --target "$target" --region "$AWS_REGION"
if git diff --quiet -- "$target"; then
  echo "$stage already references this exact image set; no PR needed." >> "$GITHUB_STEP_SUMMARY"
  exit 0
fi
git add "$target"
git -c user.name='github-actions[bot]' -c user.email='41898282+github-actions[bot]@users.noreply.github.com' commit -m "Promote tested images to $stage from $GITHUB_SHA"
git push origin "HEAD:refs/heads/$branch"
body="$RUNNER_TEMP/market-intel-release-body.txt"
cat > "$body" <<BODY
Promote the immutable Market Intel image set to $stage. Source commit: $GITHUB_SHA.

Only the three approved ECR image digests change. Review and merge to let Argo CD reconcile this environment. Staging uses a PostSync service-contract hook. Production promotion requires the operator to confirm the staged application is Synced/Healthy and the smoke hook passed.

For lasting recovery, revert this image-digest commit through Git and merge the revert. A failed production canary aborts to stable traffic; it does not rewrite Git.

GitHub-token-created PR workflows can require maintainer approval. Approve the queued PR workflows, or dispatch Test and build on the release branch, before merging. The image release already passed mandatory CI and this helper permits only image-digest edits.
BODY
if gh pr create --base main --head "$branch" --title "Promote Market Intel images to $stage" --body-file "$body"; then
  echo "Review the $stage release PR created above." >> "$GITHUB_STEP_SUMMARY"
else
  echo '::warning::PR creation was unavailable; the reviewed release branch is preserved.'
  printf 'Open the %s PR: https://github.com/%s/compare/main...%s?expand=1\n' "$stage" "$GITHUB_REPOSITORY" "$branch" >> "$GITHUB_STEP_SUMMARY"
fi
