# GHCR production deployment

The workflow in `.github/workflows/production.yml` tests the Python 3.11 suite,
builds the production image on a GitHub-hosted runner, runs the suite inside that
image with networking disabled, and then publishes it to GHCR. The deployment
job pulls the published digest and never builds on EC2.

Web, worker, and Flower use one image. Compose keeps their existing commands,
environment-file path, read-only GitHub App key mount, ports, and health check.
The `caddy` service, Caddyfile, named Caddy volumes, and external Redis and
PostgreSQL services are not selected for recreation. No migration is run by the
deployment helper.

## One-time GitHub setup

1. Create the `production` environment under repository Settings → Environments.
   Add required reviewers and restrict deployments to `main` before the first
   production dispatch. The workflow refers to this environment for both deploy
   and rollback approval.
2. Add these repository or environment secrets:
   - `EC2_HOST`: the EC2 public DNS name or IP.
   - `EC2_USER`: `ubuntu` for the current instance.
   - `EC2_SSH_PRIVATE_KEY`: the EC2 SSH login private key. Do not use the GitHub
     App private key mounted inside the application.
   - `EC2_KNOWN_HOSTS`: the verified SSH host-key line for the EC2 host. Obtain
     and verify it through a trusted channel; do not populate it from an
     unverified `ssh-keyscan` result.
3. The workflow uses its short-lived `GITHUB_TOKEN` with `packages:write` to
   publish and `packages:read` to pull. Ensure the GHCR package is linked to this
   repository or grant this repository Actions read access in the package's
   Manage Actions access settings. No long-lived GHCR token is stored on EC2.
4. Leave the repository variable `CODELENS_AUTO_DEPLOY` unset or set to `false`.
   A merge to `main` will run tests and publish the image but will not deploy.
5. Confirm EC2 has Docker Compose 2.24.4 or newer, the `ubuntu` account can run
   Docker, and the existing production Compose directory still contains `.env`,
   the mounted GitHub App PEM, and both Compose files. The current host reports
   Compose 5.5.1 and already has the required files.

## First controlled deployment

1. Do not proceed while the root filesystem has less than **1536 MiB free**.
   The latest read-only check showed about **500 MiB free**, so the helper will
   stop before creating rollback tags or pulling an image. The first GHCR image
   can require roughly the current 672 MB uncompressed application-image size,
   plus pull/unpack overhead; the helper also requires at least 300 MiB to remain
   after pulling. It does not prune images or volumes to create space.
2. Resolve the exposed local environment credentials noted in the deployment
   preparation report before starting the workflow.
3. On the `main` branch, run Actions → **Test, publish, and deploy CodeLens** →
   Run workflow → `deploy`. The image is published only after both test runs
   pass. Approve the `production` environment job only after reviewing the
   commit and published image digest.
4. The EC2 helper verifies the digest's embedded source commit and `linux/amd64`
   platform, checks free space and inodes, and requires the worker to report no
   active, reserved, scheduled, or unacknowledged Celery deliveries. If any
   inspection is unavailable or work remains in flight, it stops without
   changing services. Ready messages may remain queued in Redis; the worker
   consumes them after restart.
5. The helper tags each current service image by its exact image ID, pulls the
   GHCR digest once, checks post-pull headroom, and updates only `web`, `worker`,
   and `flower` in project `codelens`. It checks local and public FastAPI health,
   Celery worker health and ping, and Flower reachability. Failure triggers an
   attempt to restore the saved image IDs. No rollback image is removed.

The short-lived GHCR token is transferred over the verified SSH connection,
stored temporarily with mode `0600`, used through Docker's `--password-stdin`,
and removed. Docker credentials use a temporary `DOCKER_CONFIG` and are removed
when the helper exits.

## Rollback

Run the same workflow manually from `main`, choose `rollback`, and leave
`rollback_run_id` blank to restore the images saved by the latest successful
deployment. To select an earlier deployment, provide its saved deployment ID
(`GITHUB_RUN_ID-GITHUB_RUN_ATTEMPT`). The production environment approval still
applies. Rollback uses local rollback tags and performs no GHCR pull, build, or
volume operation.

## Enable automatic deployment later

After a successful controlled deployment and review, set repository variable
`CODELENS_AUTO_DEPLOY` to the exact string `true`. Future pushes to `main` then
deploy after tests and publication. Remove the environment's required-reviewer
rule only if unattended deployment is intended; otherwise every deployment
continues to wait for an approver. The workflow serializes deployments and never
cancels an in-progress deployment.

GitHub Actions and GHCR have account-specific included usage and storage limits.
Monitor the account's current quotas; immutable commit tags and rollback images
are intentionally retained rather than automatically deleted.
