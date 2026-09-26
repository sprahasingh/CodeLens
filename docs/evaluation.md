# Evaluation history

This is the full story of how I built and repeatedly broke, then fixed, CodeLens's evaluation loop. The main README keeps a compact summary; this is the detail behind it.

## Methodology

- **Chronological split.** A fixed cutoff date separates the retrieval corpus from a held-out pool of later comments, so the system can only retrieve comments that existed before the code it's being tested against was even written.
- **Stratified tune/test split.** The held-out pool is split within each repo's own timeline, not pooled across repos first, into a tuning set and a completely separate test set, with equal quotas drawn from each repo.
- **Three-stage matching:** a positional gate (same file, nearby line), then an embedding-similarity floor as a candidate filter, then an independent LLM judge that asks whether the predicted concern and the real comment raise the same underlying issue.
- **Deterministic scoring.** LLM calls run at `temperature: 0`.
- **Every run persists** as a full JSON artifact in `app/scripts/eval_runs/`, including every sample's retrieval result, synthesized concern, and the judge's verdict and reasoning.

**Cold-start**, as used throughout this doc, means the share of held-out test samples for which no retrieved candidate cleared the similarity floor at all, so the system had nothing to even compare against.

## v1: raw similarity, tuned and reported on the same data

The first version matched purely on embedding similarity between a synthesized concern and a real reviewer comment, with no independent verification step. I swept 7 thresholds against the same 15 samples I then reported the metric on. Precision came out to 37.5% at the best threshold.

This was methodologically unsound: the threshold was picked to maximize the very number being reported, so the result was optimistic by construction, not just small-sample noise.

## v2: adding the LLM judge

I added a judge stage: after a candidate cleared the similarity floor, an independent LLM call asked whether the predicted concern and the real comment actually raised the same issue. On a proper held-out test set (n=24), precision dropped to 4.7%.

Reading the judge's own reasoning showed why. Most "matches" were topically adjacent but substantively different: a predicted concern about "CI testing insufficient Python versions" matched against a real comment about "dependency caching" at 0.55+ cosine similarity, and several similar pairs. Embedding similarity between two short sentences reliably confuses same topic with same specific claim.

## v3: grounding synthesis in the actual new code

Up to this point, synthesis only ever saw the retrieved past comments, never the actual new code that triggered retrieval. I restructured the pipeline to call synthesis once per hunk, with that hunk's own code included in the prompt, plus an explicit instruction to only flag a concern if it genuinely applies to the code shown.

On a controlled tune-set comparison (same repos, same floor of 0.45), precision jumped from 23.5% to 83.3% (5 of 6 flagged concerns confirmed correct, n=14 tune samples). That's real evidence the grounding fix works.

But on the separate test set, the instruction over-corrected: synthesis returned zero concerns for the large majority of samples. Recall collapsed to functionally zero, not because the judge was rejecting bad matches, but because synthesis had become too conservative to generate anything at all in most cases.

## v4: growing the corpus, and a new methodology bug

I grew the corpus from 287 to 653 comments by adding three more repos (`pallets/flask`, `encode/httpx`, `pydantic/pydantic`) to the original two (`psf/requests`, `tiangolo/fastapi`).

The held-out pool was built by pooling every eligible comment across all repos, sorting chronologically, and splitting at the midpoint. `pydantic/pydantic` is an extremely active repo, and since ingestion pulls each repo's most recent comments, pydantic's entire slice bunched into a narrow, recent time window, dominating the late half of the pooled timeline. It ended up contributing 29 of 35 test samples (83%) on its own, against a thin pre-cutoff retrieval corpus for that repo. Cold-start spiked to 91.4%.

This was a real methodology bug, not noise: naive pooling before splitting lets one very-active repo silently take over the test set.

## v5: stratifying the split by repo

I fixed this by splitting each repo's timeline independently into its own tune-half and test-half, then drawing an equal quota from each repo rather than pooling first. Representation balanced out (no repo over 7 of 24 test samples in the following run).

Cold-start still rose to about 71% (17 of 24), though, and for a different, more interesting reason: retrieval transfers well within a topical domain (the four HTTP-client and web-framework libraries retrieve well against each other) and poorly across domains. A validation library like Pydantic doesn't retrieve well against HTTP-client code no matter how big the overall corpus gets. More corpus isn't automatically more *useful* corpus if it's not topically adjacent to what's already there.

## Determinism

Running the same 24-sample stratified test set through two separate processes at the same time (one deliberately, one by accident when I ran the same command in another terminal) produced different tp/fp counts on identical input. Groq's chat completions weren't running at a fixed temperature, so the same prompt could produce a different synthesized concern or judge verdict between calls. I added `temperature: 0` to every Groq call. Repeated calls on the same prompt now agree on the actual verdict (the boolean that drives scoring), even though the exact wording of the model's reasoning can still vary slightly, which is a known property of this kind of inference, not a bug in this codebase.

## v6: ground-truth contamination

While reading through a run's persisted results, one "real reviewer comment" being used as ground truth looked wrong: it was formatted like `⚠️ **HIGH** — *test_coverage*`, `**Confidence:** 80%`, `**Suggested fix:** ...`, which is not how a human writes a PR comment. I traced it to a GitHub account with no `[bot]` suffix, so it slipped past the existing bot filter. Checking the full 656-comment corpus at the time found exactly 3 comments matching this pattern, all from that one account.

I deleted the 3 rows and added a content-based filter (checking for markers like `**Confidence:**`) to both ingestion functions, so this can't quietly re-enter the corpus as it grows. A corpus whose entire premise is "real human reviewer feedback" can't have another automated tool's output mixed into its ground truth.

## Where this leaves things

The corpus construction, matching pipeline, and evaluation procedure are implemented and reproducible, not proven correct. Sample sizes (14-35 per run) are still small enough that any single precision, recall, or F1 number should be read as directional. The dominant bottleneck right now is retrieval cold-start on topically distant repos, not synthesis quality or judge accuracy. The two clearest next steps are deepening any one repo's pre-cutoff history (GitHub's `/pulls/comments` listing appears to cap out around 120-130 comments per repo regardless of activity level, so this would need per-PR pagination instead of the current fetch strategy) and adding repos that are topically adjacent to what's already in the corpus rather than diverse ones.
