# quintbench — experiment log

Running lab notebook for the "does a Quint skill help a coding agent" experiment.
Append-only-ish; newest findings near the top of each section. For the broader
project map see the workspace `../CLAUDE.md`.

- **Hypothesis:** giving an autonomous coding agent a Quint skill (model the
  task's state/concurrency property, check it, then code) improves pass rate on
  long-horizon SWE tasks with subtle state.
- **Harness:** `mini-swe-agent` under Pier, on the **DeepSWE** benchmark (113
  Harbor tasks). Skill repo: `github.com/jtremback/quint-skill`. Runner (this
  repo): `QuintMiniSweAgent` Pier plugin.
- **Models:** Claude on AWS Bedrock (cycles `eng-dev` account). Sonnet 4.6 is
  the workhorse; Opus 4.7/4.8 stronger arms; Haiku floors DeepSWE (0.2%) — unusable.
- **Control:** the published DeepSWE leaderboard trials (same harness, no skill)
  serve as the no-skill baseline per (task, model) — so we don't re-run control.

---

## Headline findings so far

1. **One genuine positive signal (underpowered): wazero.** Sonnet + Quint went
   **4/4** on `wazero-multi-module-snapshots` vs a 25% published baseline. Clean
   treatment-vs-control (same harness; Quint used in all 4). 4/4 vs 1/4 ≈ p≈0.06
   — suggestive, not yet significant.
2. **Quint's value is bounded by model capability.** On `kombu-SAC` (huge ~30-method
   API surface) Sonnet is **0/4 with Quint** (baseline 0%) — it models the state
   machine fine (91/94 tests) but can't implement the full surface; the failures
   are in un-modeled callback side-effects. Opus passes kombu **only when it uses
   Quint** (2/2 with, 0/2 without — confounded by self-selection). So Quint helps
   where the model can execute on the design, not where the task exceeds its reach.
3. **Invocation was the measurement-killer; now forced.** Left to choose, the agent
   used Quint ~half the time, and the within-task "Quint→pass" correlation was
   uninterpretable (self-selection). `force_quint` is now the **default**, removing
   that confound. (Override: `--agent-kwarg force_quint=false` for a control arm.)
4. **Model the side-effects, not just state.** kombu failures were the un-modeled
   `on_cancel`-fires-on-transition behavior. The passing Opus spec tracked
   `demoted`/`cancelled` as state; failing Sonnet specs didn't. Skill now nudges
   modeling effects-as-accumulated-state. (Didn't rescue Sonnet on kombu —
   modeling ≠ implementing when the surface is too big.)
5. **Sonnet is the right workhorse.** ~30% overall (real headroom), faster turns →
   completes in 13–15 min with **zero timeouts**, and triages into Quint reliably.
   Opus 4.7 burns the 90-min budget on hard tasks (timeouts = full cost, reward 0).
6. **Cost: real Bedrock ≈ 1.9× litellm's estimate.** litellm under-counts (stale
   cost map + no prompt-cache accounting); naive token×price over-counts ~2.7×
   (ignores cache reads). Budget off the `#cycles-ai-usage` daily summary.

---

## Stack build-out (eras) — what changed and why

Each era is a stack state; trials below are tagged by era. Most of the early
spend was debugging the harness, not measuring the hypothesis.

- **Era 0 — plumbing:** Bedrock auth + install pipeline. Discovered: Dockerfile
  64KB line cap (→ base64+tar, later chunked), `nodejs`/`npm` Debian conflict
  (→ install nodejs only), missing `botocore` (→ add boto3 for bedrock), SSO
  cred forwarding (mini-swe-agent only forwards the access key; SSO needs the trio).
- **Era 1 — `quint run` broken offline:** the rust evaluator is fetched from
  GitHub on first use → air-gapped sandbox fails (`EAI_AGAIN`). Agent got
  planning value only.
- **Era 2–3 — TS backend:** forced `--backend=typescript` (then a wrapper to
  force it reliably) — works offline but slow on real specs (13–92 traces/s);
  strained the timeout.
- **Era 4 — musl rust evaluator (current):** built a static `quint_evaluator`
  (musl, no glibc dep), vendored as a release asset, placed via `QUINT_HOME`.
  Fast rust (3k–9k traces/s) offline on any base image. Also: per-call LLM
  timeout (a hung call had eaten 90 min), `force_quint`, choreo framework
  vendored + working, specs persisted natively via `/logs` symlink.

---

## Trial ledger

reward 1 = passed task tests, 0 = failed/timeout. "quint" = spec written + run.
Baselines are published (no-skill, same harness) for that model+task.

### Opus 4.7 (during build-out — mostly pre-fast-rust)
| date | task | reward | quint | note |
|---|---|---|---|---|
| 06-03 | pebble / haiku | 0 | spec, run FAILED | era 1: quint run air-gap fail |
| 06-03 | pebble / opus | 0 | triaged out | timed out 90m on build loop |
| 06-03 | clack / opus | 0 | spec+run (TS) | timed out; TS slow |
| 06-03 | mobly / opus | 0 | spec+run (TS) | submitted, failed tests |
| 06-04 | **kombu / opus ×2** | **1, 1** | spec+run (fast rust) | first passes; baseline 88% |
| 06-04 | wazero / opus ×2 | 1, 0 | spec-only / wedge-timeout | |
| 06-04 | kombu / opus ×2 | 0, 0 | triaged out | both failed (no quint) |
| 06-04 | wazero / opus ×2 | 1, 0 | none / wedge-timeout | |

### Sonnet 4.6 (the workhorse)
| date | task | reward | quint | baseline | note |
|---|---|---|---|---|---|
| 06-04 | wazero ×2 | **1, 1** | spec+run | 25% | ~13m, no timeout |
| 06-04 | kombu ×2 | 0, 0 | spec+run | 0% | 91/94 — callback misses |
| 06-05 | wazero ×2 | **1, 1** | spec+run | 25% | wazero now 4/4 cumulative |
| 06-05 | kombu ×2 | 0, 0 | spec+run (effects) | 0% | still callback-bound; worse (4&8 fails) |
| 06-05 | **breadth scan ×4 tasks ×2** (forced) | _pending_ | forced | — | wazero/awilix/mobly/clack |

---

## Methodology decisions

- **Forced Quint is the default** (removes invocation noise + self-selection). Run
  it on temporal-amenable tasks only — forcing on trivial tasks wastes budget.
- **Breadth before depth.** Current open question is *generalization* (is wazero
  special?), so scan many friendly tasks at low trials and pool against baselines
  (sign test + per-task Δ histogram, per methodology.md) before going deep (8–10
  trials) on standouts to get single-task significance.
- **Don't raise the agent budget** without re-running a matched-budget control —
  the published baseline is only valid at the standard 90-min budget. Sonnet's
  speed makes raising it unnecessary anyway (it completes well within 90 min).
- **Model selection:** Sonnet for volume; Opus 4.8 to confirm on its high-headroom
  tasks; never Haiku (floored); avoid Opus 4.7 where 4.8 dominates.

---

## Open questions

- Does the wazero effect **generalize** across Sonnet's headroom band? (← the
  breadth scan addresses this.)
- Is there a measurable Δ at all once forced + pooled, vs the published baseline?
- Does "model side-effects as state" help a model that *can* implement them
  (e.g. Opus), even if it didn't rescue Sonnet on kombu?
- Cheat/regression audit (methodology.md) — not yet done.

---

## Cost ledger

litellm under-reports ~1.9×; the figures below from `#cycles-ai-usage` are real.
- June 3: **$14.96** | June 4: **$60.61** (crossed the $40 soft cap) | June 5: pending (~$20+).
- Running total through June 4: **~$75.57**; through June 5 est **~$95–100**.
- Per Sonnet trial: ~$1.9–4.1 litellm → **~$3.5–8 real**, scaling with turns
  (wazero ~$3.5, kombu-class ~$7). Opus similar per trial but wastes it on timeouts.
- Caps: $40/day soft (Slack ping), $80/day hard (auto-deny until 00:00 UTC).
  Raise via `users.yaml` in cycles-aws-bedrock. Self-service spend query: see
  cycles-aws-bedrock issue #25.

---

## Inspecting runs (tooling)

- `pier view jobs` — web trajectory browser (agent's turn-by-turn reasoning).
- `python scripts/extract-review.py [filter]` → `review/<task>/<trial>/` with
  RESULT.txt + model.patch + specs/*.qnt (specs reconstructed from trajectory).
- `python scripts/reconstitute.py <trial-dir> --gold` → `recon/<task>/<trial>/`
  the agent's full repo filesystem (base commit + model.patch), gold sibling for
  `diff -ru gold repo`.
- New runs persist specs natively at `jobs/.../agent/quint-specs/`.
