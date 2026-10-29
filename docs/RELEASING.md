# Release preparation

Use a focused branch and record the exact candidate commit. A passing check on an earlier commit or a successful container start does not validate the final release.

1. Integrate reviewed changes in dependency order and resolve outstanding release blockers. Retain the current production image and its migration path.
2. Keep release notes marked as an unreleased draft until validation is complete. Check application, installer, README and image versions together. Run `tools/prepare_release.py` through the release workflow; do not override an existing version tag.
3. Run the CI test suite, import checks, blocking Ruff rules and migration scenarios against the final candidate. Review all production code, dependencies, authentication, secret handling, storage/restore and publication workflows. Record skipped checks and remaining risks explicitly.
4. Build and inspect both supported image architectures. Verify runtime identity, source/license inclusion and absence of local credentials, logs, databases and backups. Exercise the candidate on an operator-approved trial and collect its activation identity and complete runtime cycles.
5. Review upgrade and rollback instructions, including panel credentials and TLS configuration. Changes in defaults must be called out before installation.
6. Publish only from the gated release workflow for the verified commit. Check the resulting release archive, immutable image identity and install/upgrade behavior. Update stable installation links after publication.

For 1.7.1, trial validation must include user synchronization and API collection ordering, node/user failure isolation, preservation of skipped warnings, and real API-outage recovery. Security fixes need validation on the final combined candidate even when earlier runtime trials passed.

## Repository identity and independence

The canonical project is [JavadMoqaddam/PG-Limiter](https://github.com/JavadMoqaddam/PG-Limiter), with images at `ghcr.io/javadmoqaddam/pg-limiter`. Keep installer URLs, image labels and repository links consistent. Preserve the AGPL license, original copyright notices, Git history and the upstream credits in the README.

Suggested repository description: **Connection limit enforcement for PasarGuard, with Telegram administration and API/SSE monitoring.** Enable private vulnerability reporting before advertising a direct private-report workflow, and check required CI/review protections and Actions permissions in repository settings.

GitHub's fork relationship is hosting metadata; changing source files or credits does not detach it. Before any detachment, inventory open PRs, issues, releases, discussions, repository settings and package access, and export the records that must be retained. Integrate or preserve open PR work first.

GitHub documents fork detachment as permanent and warns that issues, PRs, stars, watchers and other repository metadata may not be retained. Review the current [official detachment instructions](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/working-with-forks/detaching-a-fork), confirm the available method and its effects, and obtain the owner's explicit approval before executing it. Do not delete/recreate the repository or mirror-push as an incidental release-cleanup step.

After an approved hosting change, recheck repository identity, private reporting, branch protections, Actions secrets/variables, GHCR linkage, release assets and installation URLs. Independence does not remove the license or attribution obligations of inherited code.
