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

## Temporary GitHub Actions SSH access with AWS OIDC

The EC2 security group sg-05c1462a133b8d00f is managed by a dedicated
GitHub Actions role. For deploy and rollback runs, the workflow obtains a
short-lived AWS session through GitHub OIDC, reads the current runner IPv4 from
https://checkip.amazonaws.com/ over verified HTTPS, validates that the
response is exactly one globally routable IPv4 address, and adds one TCP/22
/32 ingress rule. The current fixed rule TTL is two hours. The helper stores
the AWS-returned sgr-... rule ID as a job output and passes that exact ID to
the cleanup command. It never searches by or revokes the Mac rule
sgr-070a3d2767818f890; the existing Mac, HTTP, and HTTPS rules are otherwise
left alone.

The revoke helper first describes the requested rule in the fixed security
group and checks its group, rule ID, direction, TCP/22 ports, public /32 source,
management tags, description, and this workflow run identity. It then calls
revoke-security-group-ingress with that one AWS rule ID and verifies the rule
is absent. Errors are reduced to fixed messages. A failed revoke fails the job.
The workflow's always() cleanup step handles normal failures and cancellation.
A scheduled job runs every 15 minutes in the separate
production-ssh-recovery GitHub environment and removes only expired rules with
the exact CodeLens owner/run/expiry tags and matching run description. The
recovery environment must be restricted
to main, have no secrets, and have no reviewer gate so cleanup can run
unattended. It receives the same narrowly scoped AWS role. A manual recover_ssh
dispatch from main is also available.

### GitHub setup

1. Keep the existing protected production environment and its required
   reviewers. Its deployment branches must remain restricted to main.
2. Create production-ssh-recovery. Restrict its deployment branches to main;
   add no secrets and no required reviewers. It is used only by the 15-minute
   stale-rule reaper and manual recover_ssh operation.
3. Add repository variable CODELENS_AWS_ROLE_ARN with the ARN of the IAM role
   described below. It is an ARN, not a credential. Do not add AWS access keys.
4. Keep EC2_HOST, EC2_USER, EC2_SSH_PRIVATE_KEY, and EC2_KNOWN_HOSTS secrets
   only in production. No EC2 SSH secret is used by the recovery job.
5. The IAM OIDC provider URL is
   https://token.actions.githubusercontent.com, with audience sts.amazonaws.com.
   Add the provider only if it does not already exist.

The verified GitHub repository OIDC settings are `use_default: true`,
`use_immutable_subject: true`, and immutable subject prefix
`repo@99238925/CodeLens@1334470875`. No custom include-claim-key list is
configured. With an environment, the exact subject values are
`repo:sprahasingh@99238925/CodeLens@1334470875:environment:production` and
`repo:sprahasingh@99238925/CodeLens@1334470875:environment:production-ssh-recovery`.
GitHub documents this immutable `repo:OWNER@OWNER-ID/REPO@REPO-ID` syntax and
the `:environment:ENVIRONMENT` suffix for environment jobs. The trust policy
below accepts only those two subjects, the expected audience, repository and
owner IDs, and `main`.

### IAM role trust policy

Create the IAM role `CodeLensGitHubActionsSSH` and use this trust policy.
The account and GitHub IDs below are the verified values. The OIDC provider
must exist in AWS account `510155707736`, region `eu-north-1`.

    {
      "Version": "2012-10-17",
      "Statement": [
        {
          "Effect": "Allow",
          "Principal": {
            "Federated": "arn:aws:iam::510155707736:oidc-provider/token.actions.githubusercontent.com"
          },
          "Action": "sts:AssumeRoleWithWebIdentity",
          "Condition": {
            "StringEquals": {
              "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
              "token.actions.githubusercontent.com:repository_owner_id": "99238925",
              "token.actions.githubusercontent.com:repository_id": "1334470875",
              "token.actions.githubusercontent.com:ref": "refs/heads/main",
              "token.actions.githubusercontent.com:environment": [
                "production",
                "production-ssh-recovery"
              ],
              "token.actions.githubusercontent.com:sub": [
                "repo:sprahasingh@99238925/CodeLens@1334470875:environment:production",
                "repo:sprahasingh@99238925/CodeLens@1334470875:environment:production-ssh-recovery"
              ]
            }
          }
        }
      ]
    }

### IAM permissions policy

Attach only this inline policy to that role. The helper authorizes rules with
`TagSpecifications` on the `AuthorizeSecurityGroupIngress` API call; it does
not make a separate `CreateTags` API call. EC2 performs dependent authorization
for `ec2:CreateTags` when tags are supplied during rule creation. This policy
allows that tag-on-create path only, limits ingress authorization and
revocation to the CodeLens security group, and permits read-only rule
inspection in `eu-north-1`. It grants no instance, key, network-interface, or
other security-group management permissions.

    {
      "Version": "2012-10-17",
      "Statement": [
        {
          "Sid": "DescribeSecurityGroupRules",
          "Effect": "Allow",
          "Action": "ec2:DescribeSecurityGroupRules",
          "Resource": "*",
          "Condition": {
            "StringEquals": {"ec2:Region": "eu-north-1"}
          }
        },
        {
          "Sid": "AuthorizeIngressOnCodeLensGroup",
          "Effect": "Allow",
          "Action": "ec2:AuthorizeSecurityGroupIngress",
          "Resource": "arn:aws:ec2:eu-north-1:510155707736:security-group/sg-05c1462a133b8d00f",
          "Condition": {
            "StringEquals": {
              "ec2:SecurityGroupID": "sg-05c1462a133b8d00f",
              "ec2:Region": "eu-north-1"
            }
          }
        },
        {
          "Sid": "AuthorizeTaggedRuleCreation",
          "Effect": "Allow",
          "Action": "ec2:AuthorizeSecurityGroupIngress",
          "Resource": "arn:aws:ec2:eu-north-1:510155707736:security-group-rule/*",
          "Condition": {
            "StringEquals": {
              "aws:RequestTag/CodeLensManagedBy": "CodeLensGitHubActions",
              "ec2:Region": "eu-north-1"
            },
            "ForAllValues:StringEquals": {
              "aws:TagKeys": ["CodeLensManagedBy", "CodeLensRun", "CodeLensExpiresAt"]
            },
            "Null": {
              "aws:RequestTag/CodeLensRun": "false",
              "aws:RequestTag/CodeLensExpiresAt": "false"
            }
          }
        },
        {
          "Sid": "TagOnlyRulesCreatedByIngressAuthorization",
          "Effect": "Allow",
          "Action": "ec2:CreateTags",
          "Resource": "arn:aws:ec2:eu-north-1:510155707736:security-group-rule/*",
          "Condition": {
            "StringEquals": {
              "ec2:CreateAction": "AuthorizeSecurityGroupIngress",
              "aws:RequestTag/CodeLensManagedBy": "CodeLensGitHubActions",
              "ec2:Region": "eu-north-1"
            },
            "ForAllValues:StringEquals": {
              "aws:TagKeys": ["CodeLensManagedBy", "CodeLensRun", "CodeLensExpiresAt"]
            },
            "Null": {
              "aws:RequestTag/CodeLensRun": "false",
              "aws:RequestTag/CodeLensExpiresAt": "false"
            }
          }
        },
        {
          "Sid": "RevokeIngressOnlyOnCodeLensGroup",
          "Effect": "Allow",
          "Action": "ec2:RevokeSecurityGroupIngress",
          "Resource": "arn:aws:ec2:eu-north-1:510155707736:security-group/sg-05c1462a133b8d00f",
          "Condition": {
            "StringEquals": {
              "ec2:SecurityGroupID": "sg-05c1462a133b8d00f",
              "ec2:Region": "eu-north-1"
            }
          }
        }
      ]
    }

AWS authorizes ingress authorization and revocation at the security-group
level; it does not let IAM constrain a request to TCP/22, a /32, or one specific
rule ID. `ec2:SecurityGroupID` is applied only to the security-group resource
authorization check; AWS lists request-tag keys for the separate
security-group-rule resource check. The helper enforces the port, /32, and
rule-ownership checks, revokes by exact rule ID, and explicitly protects both
the known Mac rule ID and its current /32 source. A compromised workflow role
could still add another ingress rule or revoke another rule in this one group;
isolating runner SSH rules in a dedicated security group would remove that
IAM-level limitation but requires a separately approved infrastructure
change. `DescribeSecurityGroupRules` has no resource-level ARN, so its read
permission uses `Resource: "*"`; the region condition is the narrowest IAM
scope available, while the helper filters requests to the CodeLens group.

### Retry and recovery

After configuring the role, provider, repository variable, and recovery
environment, rerun Actions → Test, publish, and deploy CodeLens → Run workflow
from main with operation=deploy_existing and the same verified publication
run ID, source commit, and image digest. Do not select deploy; that mode
publishes a new image. The workflow still runs tests, validates the previous
successful publication, waits for production approval, then applies the
existing Celery idle gate and deployment-helper rollback protections.

On the next scheduled recovery run, expired managed rules are removed. If
cleanup fails, inspect the sanitized recovery result and manually dispatch
recover_ssh from main; do not remove a rule by CIDR or description. If an
expired managed rule cannot pass validation, the reaper fails closed and
requires manual inspection of that exact tagged rule. No automated process
can guarantee cleanup during a prolonged GitHub Actions outage; the two-hour
expiry marker and scheduled reaper bound expected exposure when Actions is
available.
