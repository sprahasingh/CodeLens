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
   The EC2 EBS volume was expanded to **12 GiB** and the root filesystem was
   grown online. A subsequent check showed about **4.37 GiB free** (the exact
   amount changes as Docker images and logs grow). The helper still checks the
   live free space and inodes before changing anything; it requires 1536 MiB
   before a pull and at least 300 MiB after it. It does not prune images or
   volumes to create space.
2. The previously exposed credential categories have been rotated and the old
   credentials revoked. Keep the replacement values only in their protected
   provider settings and production `.env`; never paste or print them in Actions
   logs or deployment diagnostics.
3. For the already-published first-release image, open Actions → **Test, publish,
   and deploy CodeLens** → **Run workflow**, select branch `main`, and set:
   - `operation`: `deploy_existing`
   - `publication_run_id`: the numeric run ID from the successful publication
     run's URL (not the UI run number). For the reported run #9, copy the ID from
     its `/actions/runs/<id>` URL.
   - `source_commit`:
     `b3c7a3cc74336272b907c10eb32ca874aaa908cb`
   - `image_digest`:
     `sha256:c2da031dca76bccab86e95b05a469048dc876d926c5ba978a0e0e41fba7b2e3a`
   The verification job checks that this is a successful main publication from
   this workflow, that its test and image-publish steps succeeded, that the
   commit tag resolves to the supplied immutable digest, and that the image
   config identifies the same source commit and `linux/amd64`. It reads registry
   manifests/config only; it does not pull, rebuild, or republish the image.
4. The `deploy_existing` mode runs the current workflow's tests and then the
   verifier. It skips the publisher, so it cannot collide with or overwrite the
   source commit tag. The existing `deploy` mode remains for a newly unpublished
   main commit: it tests, builds, publishes, and deploys that same run's digest.
5. Confirm the `production` environment has a required reviewer. Approve the
   deployment job only after checking the source commit and digest in the run.
   Keep `CODELENS_AUTO_DEPLOY` absent or `false`.
6. Immediately before approval, confirm the deployment's Celery gate is clear:
   active, reserved, scheduled, unacknowledged, and unacknowledged-index counts
   must all be zero. If inspection is unavailable or any count is nonzero, the
   helper aborts before changing services.
7. The EC2 helper verifies the digest's embedded source commit and `linux/amd64`
   platform, checks free space and inodes, and requires the worker to report no
   active, reserved, scheduled, or unacknowledged Celery deliveries. If any
   inspection is unavailable or work remains in flight, it stops without
   changing services. Ready messages may remain queued in Redis; the worker
   consumes them after restart.
8. The helper tags each current service image by its exact image ID, pulls the
   GHCR digest once, checks post-pull headroom, and updates only `web`, `worker`,
   and `flower` in project `codelens`. It checks local and public FastAPI health,
   Celery worker health and ping, and Flower reachability. Failure triggers an
   attempt to restore the saved image IDs. No rollback image is removed.

The short-lived GHCR token is transferred over the verified SSH connection,
stored temporarily with mode `0600`, used through Docker's `--password-stdin`,
and removed. Docker credentials use a temporary mode-`0700` `DOCKER_CONFIG` and
are removed when the helper exits, including failure and handled-signal paths.
Authentication, pull, task-inspection, and Compose errors are reported using
fixed sanitized messages; raw command output is not forwarded to Actions logs.

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
