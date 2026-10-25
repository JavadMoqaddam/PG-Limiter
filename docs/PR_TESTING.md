# Testing a reviewed PR with Docker

The GitHub PR workflow validates the Docker build, but its `pull_request` run does not publish to GHCR. A successful build does not provide a pullable PR tag. A local build of the reviewed commit allows a trial before merging.

Use the reviewed operator-provided trial and rollback procedure for activation. It should freeze a full commit SHA, build before stopping the existing service, and start the prepared container only after verification.

## Build and identify the image

The build portion of the trial should follow this pattern. Replace the PR number and commit with the reviewed values; retarget the frozen commit after any further PR changes.

```bash
set -euo pipefail
trial_pr_number=37
trial_commit=REPLACE_WITH_REVIEWED_40_CHARACTER_SHA
trial_checkout="pg-limiter-pr${trial_pr_number}"
trial_tag="pg-limiter-pr${trial_pr_number}:latest"
[[ "$trial_commit" =~ ^[0-9a-f]{40}$ ]] || exit 1

git clone https://github.com/JavadMoqaddam/PG-Limiter.git "$trial_checkout"
git -C "$trial_checkout" fetch origin "pull/${trial_pr_number}/head"
test "$(git -C "$trial_checkout" rev-parse FETCH_HEAD)" = "$trial_commit"
git -C "$trial_checkout" checkout --detach "$trial_commit"
test "$(git -C "$trial_checkout" rev-parse HEAD)" = "$trial_commit"

docker build --label "org.opencontainers.image.revision=$trial_commit" \
    --tag "$trial_tag" "$trial_checkout"
docker image inspect --format \
    'id={{.Id}} revision={{index .Config.Labels "org.opencontainers.image.revision"}}' \
    "$trial_tag"
```

Record the immutable local image ID and verify its revision label. The operator-provided trial override must use that ID, rather than relying on the mutable `latest` tag.

## Activate and preserve state

The operator-provided trial procedure must:

1. Record the existing container image, actual Compose project/files, persistent mounts, environment and relevant limits privately. Cache the old image for rollback.
2. Complete the new image build before stopping the service. Take a private cold backup of configuration and state, excluding logs and existing backups; keep credentials and backup contents out of issues or PRs.
3. Use the installed base Compose file plus a new trial override only. Remove old `/app` source binds so the container runs the full built image. Preserve `.env`, configuration, storage mounts and operational overrides such as replay capacity and memory/swap limits.
4. Generate rollback from the recorded Compose files and cached old image. Before the first stop, verify its rendered configuration against the existing image, environment, mounts and limits. Recheck recorded files and source binds before stopping for rollback. Preserve the current database and warning/history state; restoring a backup is a separate decision. An older preparation script with a fixed image guard cannot perform this rollback.
5. Verify the prepared container is stopped, its immutable image/revision are correct, persistent mounts are unchanged and no `/app` source mounts remain. Apply the warning protection below, then start last.

Do not run `pg-limiter update` during the trial: it selects the published release and can replace the test image.

Verify a protective effective warning threshold before startup, preserving counters and histories. The trial safeguard should require at least 100 warnings and a margin of ten scans above the highest saved streak. An already safe setting can be verified read-only; changing it requires a private audit of prior settings. Verify the effective database setting because its override can take precedence over ENV. Do not perform Trust Reset or reset counters/history. Review saved warnings before restoring the threshold; existing records can retain their creation threshold.

## Verify the intended source and workload

Choose **Per Node** first, then **API**. Confirm the settings committed and a new user synchronization succeeded. Observe an actual fresh API collection cycle with a nonempty workload; record completion, successful/failed requests, unknown users and node coverage. Start success, cached results or an empty cycle alone do not establish working enforcement. Partial or unknown coverage must remain explicit.

API online-context timestamps and accepted log timestamps can represent connection start. A short freshness setting measures recent openings and can exclude a long-lived transmitting connection; it does not measure ongoing traffic. Keep this limitation separate from CPU and request-load validation.

Manual CPU profiling is a separate action. The diagnostic provided separately for this trial samples for 90 seconds and stops that same container automatically. Record the tested image/revision, workload and coverage with the profile, and expect zero source binds for a full-image trial. Review the resulting evidence before claiming production recovery.
