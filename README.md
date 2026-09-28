# CodeLens

Historical PR review retrieval and code-grounded pre-review feedback for GitHub, evaluated end to end.

**Live:** [13-51-158-45.sslip.io](https://13-51-158-45.sslip.io) · **GitHub App:** [github.com/apps/codelens-gh](https://github.com/apps/codelens-gh)

I built this because I kept running into the same problem: you open a PR and wait for a senior engineer to point out something that's already been flagged repeatedly on similar code. CodeLens indexes historical PR review comments, grounds them to the exact code they were left on, and posts a synthesized pre-review briefing when a new PR opens. I backed it with a real, measured evaluation loop instead of just claiming it works.

## Contents

- [The core idea](#the-core-idea-hunk-grounded-retrieval)
- [Architecture](#architecture)
- [Example output](#example-output)
- [Tech stack](#tech-stack)
- [Evaluation](#evaluation)
- [Known limitations](#known-limitations)
- [Why Groq](#why-groq)
- [Repository structure](#repository-structure)
- [Running it](#running-it)
- [Scaling this further](#scaling-this-further)

## The core idea: hunk-grounded retrieval

My first instinct was to embed the review comment text itself and search over that. It doesn't work well. It retrieves generic advice like "add a test" or "handle the error" with no way to tell whether that advice actually applies to the specific new code being reviewed.

GitHub's review-comment API pairs every historical comment with the exact code it was left on, its `diff_hunk`. So I index `(diff_hunk -> comment)` pairs and embed the code itself, not the comment text. When a PR opens, each changed hunk gets embedded the same way and matched against this corpus. Retrieval is grounded in code similarity, and synthesis is grounded in the actual new code, not just whatever comments got retrieved in isolation.

## Architecture

```mermaid
flowchart TD
    A[PR opened / pushed] --> B[webhook: signature + idempotency]
    B --> C{synchronize?}
    C -- yes --> D[delta diff: before...after SHA]
    C -- no --> E[full PR diff]
    D & E --> F[split into hunks · filter noise]
    F --> G[fan-out: N × process_pr_batch in parallel]
    G --> H[embed with voyage-code-4 · pgvector search]
    H --> I[LLM synthesis · Groq]
    I --> J[post PR comment with findings + evidence links]
```

Large PRs are split into independent batches of 10 hunks and queued simultaneously. Each batch posts its own comment as soon as it finishes, so feedback from the first completed batch typically appears within 30 seconds. On `synchronize` events (new commits pushed to an open PR), CodeLens uses GitHub's three-dot compare endpoint to scan only the newly pushed commits, not the entire PR diff again. If a batch hits its time limit, whatever was synthesized gets posted immediately with a partial indicator so nothing is lost silently.

The posted comment shows its work. Every finding links back to the specific historical GitHub comment that informed it, providing provenance for the generated feedback.

### What the pipeline does

- Ingests historical PR review comments paired with their `diff_hunk`
- Embeds historical and new PR hunks with `voyage-code-4` and retrieves matches above the similarity floor
- Generates findings grounded in the actual new code
- Links findings to the historical GitHub comments used as evidence
- Fans out large PRs into parallel batches of 10 hunks
- Processes only newly pushed commits on `synchronize` events
- Evaluates against held-out reviewer comments using an independent LLM judge

## Example output

This is real: a hunk from an actual `tiangolo/fastapi` PR, run through the current pipeline against the real corpus (not posted to GitHub, just generated locally to show the format).

The changed code:

```yaml
-          version: "0.11.18"
+          version: "0.11.30"
+          cache-suffix: dev-all
```

What CodeLens produced:

> ### Finding 1: Adding a cache-suffix without enabling the cache may leave caching disabled for this job *(inferred)*
> **Confidence:** 92% | **Suggested check:** Confirm that `enable-cache: true` is set (or that caching is otherwise enabled) for this step
>
> **Similar past code** — `.github/workflows/smokeshow.yml` (similarity: 88%):
> ```yaml
> cache-suffix: github-actions
> ```
> **Past reviewer said:** *"Since setup-uv v10.0.0, cache for this workflow is disabled... I think we can safely enable it..."* ([view original](https://github.com/fastapi/fastapi/pull/16152#discussion_r3864473925))
>
> **Evidence:** Past comment notes that after setup-uv v10.0.0 the cache is disabled unless `enable-cache: true` is set, and only a cache-suffix was added here.

## Tech stack

| Tool | Role |
|---|---|
| FastAPI | Webhook ingestion, API endpoints, landing page |
| PostgreSQL + pgvector | Vector search over code embeddings (Neon, free tier) |
| SQLAlchemy + Alembic | ORM and schema migrations |
| Voyage AI | Embeddings: `voyage-code-4` for code retrieval, `voyage-4-lite` for natural-language matching |
| Groq | LLM for synthesis and the evaluation judge (free tier, no billing account required) |
| Celery + Redis | Background job queue with parallel batch fan-out (Upstash, free tier) |
| GitHub App + Webhooks | Auth, event delivery, HMAC-SHA256 signature verification |
| ntfy | Push notifications on new app installations |
| structlog | Structured logging with request-id tracing |
| Docker + Compose | One-command local setup |
| pytest | Unit + integration tests |

Every piece runs on a genuinely free tier. See [Why Groq](#why-groq) for why the LLM provider changed mid-project.

## Evaluation

I wanted a way to actually check whether this system works, not just claim it does, so CodeLens measures itself against a held-out set of real historical review comments from maintainers who have never seen CodeLens, using a chronological split, a stratified tune/test split by repo, and an independent LLM judge as the final match decision, not raw similarity. Full method and the complete run-by-run history are in [`docs/evaluation.md`](docs/evaluation.md); every run is also persisted as a full JSON artifact in `app/scripts/eval_runs/`.

**Cold-start** here means the share of held-out test samples for which no retrieved candidate cleared the similarity floor at all, so the system had nothing to compare against.

| Version | Change | Result |
|---|---|---|
| v1 | Raw similarity, threshold tuned and reported on the same 15 samples | 37.5% precision, methodologically optimistic |
| v2 | Added an independent LLM judge | 4.7% precision (n=24 test set) |
| v3 | Grounded synthesis in the actual new-PR code | 83.3% precision on the tune subset (5/6 judged matches; 14 total samples), but recall collapsed on the test set |
| v4 | Grew the corpus from 287 to 653 comments across 5 repos | A naive pooled split let one very-active repo eat 83% of the test set |
| v5 | Stratified the split by repo | Balanced representation, but cold-start rose to ~71% (17 of 24) |
| v6 | Found and removed 3 ground-truth comments that were another automated tool's output | Corpus integrity fix, filter added to prevent recurrence |

Where things actually stand: the corpus construction, matching pipeline, and evaluation procedure are implemented and reproducible, not proven correct. Sample sizes (14-35 per run) are still small enough that any single precision, recall, or F1 number should be read as directional. The dominant bottleneck right now is retrieval cold-start on topically distant repos, not synthesis or judging quality.

## Known limitations

- **Cold-start, around 70%.** Most held-out test hunks retrieve nothing above the similarity floor. That's a corpus-coverage problem, not a matching-quality problem.
- **Domain transfer is weak.** Code-similarity retrieval works within a topical neighborhood, like web frameworks or HTTP clients, and doesn't reliably transfer to a structurally different domain such as a validation library.
- **Small sample sizes.** 14 to 35 samples per run means any single number carries a wide confidence interval.
- **Corpus coverage.** The current ingestion strategy does not reliably capture every historical review comment in highly active repositories, which limits corpus coverage.
- **`temperature: 0` reduces but doesn't guarantee bit-for-bit determinism.** Verdicts are stable across repeated calls; exact wording occasionally isn't, which is a known property of this kind of inference, not a bug here.

## Why Groq

I initially built this on Gemini, but an account-level access restriction required paid billing to continue using the API. Since keeping the project zero-cost was a design constraint, I moved the LLM layer to Groq's free, rate-limited tier.

## Repository structure

```
app/
  core/       config, database connection, Celery app, middleware
  models/     SQLAlchemy models (repos, review comments, predictions, false negatives, processed PRs)
  routers/    FastAPI routes (repos, webhook, metrics)
  services/   retrieval, synthesis, ingestion, evaluation, GitHub client/auth, LLM client
  tasks/      Celery task definitions (process_pr fan-out, process_pr_batch)
  scripts/    backfills, corpus ingestion, and the held-out evaluation script
  static/     landing page
alembic/      database migrations
tests/        pytest suite
docs/         evaluation history and other detail moved out of the main README
```

## Running it

### Locally

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your own values
alembic upgrade head
uvicorn app.main:app --reload
```

### With Docker

```bash
docker compose up --build
```

Runs the API on `:8000`, a Celery worker, and Flower on `:5555`. It connects out to your existing managed Postgres and Redis (Neon and Upstash) through `.env`, so there are no local database containers, which matches how I actually run this. The GitHub App private key gets mounted read-only from the repo root; update the filename in `docker-compose.yml` if the key ever gets rotated.

### Tests

```bash
pytest
```

Covers diff parsing, the matching and scoring engine, markdown formatting, synthesis output filtering (including a regression test for a negative-index bug I found while writing these), and a DB-free integration test of the webhook and health endpoints.

## Scaling this further

I deliberately left out Kafka, Kubernetes, an MCP server, ReAct agents, and multi-tenancy to keep the zero-cost constraint and go deep on retrieval and evaluation instead of breadth. At real scale I'd add durable event buffering and worker autoscaling for burst handling, per-tenant corpus isolation, a `.codelensignore` per repo to filter noise like lockfiles, and a proper A/B framework for prompt and threshold changes instead of the sequential eval-and-compare loop I'm running now.
