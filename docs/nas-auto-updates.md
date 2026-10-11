# Tested NAS Updates

This fork keeps personal records, backups and secrets on the NAS. They must not
be committed or uploaded as CI artifacts.

## Release Pipeline

`Tested NAS Update` runs weekly on Monday at 10:23 UTC and after a push to `main`.
It can also be started manually in Actions or with GitHub CLI.

1. Fetch `cocdeshijie/plan.cards` and merge it without force-pushing.
2. Stop on merge conflicts or upstream changes to CI/deployment controls.
3. Test the exact candidate: backend tests, deployment tests, frontend typecheck,
   lint and production build; build both runtime images.
4. Confirm that `main` has not moved during testing; fast-forward it to the
   tested candidate and publish a `nas-<full commit SHA>` release.

Test/build jobs have read-only repository permissions. Only the isolated publish
job has write permission. No NAS credentials are stored in GitHub.

## NAS Deployment

Install a reviewed copy of `tools/nas-update/deploy.py` outside the release
directory. It never replaces itself automatically.

The included systemd timer checks for a new tested release hourly and five
minutes after NAS boot. It does not need this desktop or the Codex app to be open.
Install the three unit files under `/etc/systemd/system`, reload systemd, and enable
the timer only after a successful supervised deployment. Confirm it remains
installed after major DSM upgrades.

The failure unit sends a DSM notification to the administrators group. Email or
mobile delivery depends on the NAS notification settings; it is not configured
by this installer. Build/merge failures appear in GitHub Actions.

Configuration example (local only):

```json
{
  "enabled": false,
  "repository": "OWNER/plan.cards",
  "base": "/volume1/docker/plan-cards",
  "docker": "/usr/local/bin/docker",
  "compose": "/usr/local/bin/docker-compose"
}
```

The installer/operator must verify the owner, backend data mount and frontend
port before enabling deployment. Use `--check` for a read-only release check.
Only the existing frontend port 32730 is published; the backend stays internal.

The updater downloads immutable images and templates, validates SHA-256 hashes
and image revision labels, then stops the application briefly. It backs up the
database, encryption/signing keys, local configuration and previous image IDs
before starting the new backend. It checks SQLite integrity, foreign keys,
existing record IDs and key hashes before bringing the frontend back online.

On failure it retains the failed data as well as the pre-update backup and
restarts the old images. A failed release is not retried automatically. A failed
rollback or interrupted deployment requires inspection; do not clear its state
without checking the backup and current containers.

Backups live under `auto-update/backups`. They are not automatically deleted.
If users wrote records during a failed frontend cutover, inspect `failed-data`
before deleting any backup; those writes may need reconciliation.

## Operations

```sh
python3 /volume1/docker/plan-cards/auto-update/deploy.py \
  --config /volume1/docker/plan-cards/auto-update/config.json --check
```

Set `enabled` to `false` in the NAS configuration to stop deployment. GitHub
Actions can still build releases without changing the running tracker.

The deployment configuration is independent of the upstream Compose files and
retains the current container environment. Changes to ports, mounts, environment
requirements or the updater itself require deliberate review.

GitHub may disable a scheduled workflow in an inactive public repository. Check
the Actions page if scheduled runs stop; workflow dispatch is always available
while Actions is enabled.
