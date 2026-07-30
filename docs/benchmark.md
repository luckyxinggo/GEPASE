# GEPASE Public Skill Package Benchmark v1

> Status (2026-07-20): retained only as an integration/calibration fixture. It is not an active
> optimization benchmark and its 1.0 executable-assertion results are not Skill quality scores.

Benchmark v1 contains 150 TaskCases over three independently authored Apache-2.0 Skill
Packages: structured HTML reporting, bounded tabular context construction, and policy evidence
evaluation. Each package contributes 50 cases. Leakage-group-aware splits contain 90 train, 30
validation, and 30 isolated test cases.

## Scoring and evidence

Every case assigns 0.8 weight to deterministic assertions and at most 0.2 to the blind quality
rubric. Final functional acceptance requires E3 executable assertions. E1 is a planned-only proxy:
it is useful for broad screening, but cannot establish that an artifact was produced correctly.

S1-B generated 300 independent DeepSeek V4 Pro E1 simulations (no-skill/original for every
case). The model's raw self-estimated success was rejected as the primary metric because it placed
two Skills above the 0.90 ceiling and disagreed with E3 direction on two pre-registered cases. The
frozen proxy is `deterministic-plan-quality-v1`: eight structural plan dimensions multiplied by an
E1 epistemic reliability cap of 0.85. Raw self-scores remain in the task-level records and
calibration ledger for auditability.

The high-fidelity calibration subset contains six blind Agent-native pairs, two per Skill. It has
12 E2 delegated records and 12 E3 derived records. In this subset original Skills scored 1.0 on
all executable checks, while no-skill baselines scored from 0.4 to 0.667. These six cases are a
smoke/calibration result, not a statistically powered optimization claim.

## Assertion quality

The benchmark has 650 deterministic assertions across `file_exists`, `file_contains`,
`json_equals`, and `html_contract`. Mutation testing injected 1,300 missing-file, truncated-file,
wrong-value, wrong-type, missing-contract-node, and remote-asset faults. All four assertion
families achieved a 1.0 kill rate. This result validates the registered mutation catalog; it does
not prove coverage of every possible semantic error.

## Freeze and access policy

`benchmarks/freeze-v1.lock.json` records the manifest, split, dataset, rubric, and package
provenance hashes. The test split policy is `isolated-read-only`. E1 workers receive task prompts,
fixtures, and (for original only) the public Skill context, but not assertion specs, expected labels,
candidate identity, or optimizer metadata.

## Evolution development track

`benchmarks/evolution-v1/` is a separate, transparently labeled repair-development track. It does
not alter Benchmark v1 or its freeze lock. Every fault family records its origin, repair
provenance, oracle, leakage group, and allowed split. The initial structured-report fault models a
missing source footer and exists to test optimization observability and causal repair mechanics,
not to masquerade as a naturally sampled production defect.

## Withdrawn local-real action benchmark

The former local-real development profiles reduced heterogeneous Skill behavior to fixed
`expected_action` labels and allowed the framework to synthesize `result.json`. That path was
withdrawn and deleted on 2026-07-20. None of its records, readiness decisions, candidates, or scores
are valid evidence of functional Skill quality.

Its replacement is specified in `state.md` R1-R5: reviewed trigger cases, task-native functional
outputs, same-round with-skill/baseline Agent execution, independent grading, composite score
vectors, full package filesystem access, and strict-improvement admission.

## Graph-hardening canary track

GH-E1 is not a new benchmark split and does not change Benchmark v1. It is a separate application
run on the pinned public `slack-gif-creator` Package using the already frozen 5-train/3-validation
EvalPlan. It rebuilt a fresh paired reference, evaluated two graph-guided bounded patches, and
ended with `no_strict_improvement`: one branch regressed at train; the train-admitted branch had a
positive held-out mean but crossed a protected category floor. This negative result is retained
because the benchmark contract forbids changing cases, weights, thresholds, or the candidate after
seeing validation.

## Known limitations

- The data is synthetic but non-toy and license-clean; external validity on private production
  Skills is a later local-only experiment.
- The E1 plan metric measures structural readiness, not execution success. Search acceptance must
  still use E2/E3 at the pre-registered tier.
- The current E3 calibration subset has only six pairs and is ceilinged. It must not be expanded
  into a repeated-seed matrix; a new headroom-audited benchmark is required first.
- Judge quality has not yet been used for an optimization claim; deterministic assertions dominate
  all case scores.
- Benchmark v1 tasks were co-designed with deterministic scripts and therefore test narrow output
  contracts more strongly than overall Skill usefulness, triggering quality, or professional output
  quality.
