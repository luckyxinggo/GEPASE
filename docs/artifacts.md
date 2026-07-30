# Artifact Contract

Artifacts are written atomically and indexed by relative path, SHA-256, media type, and byte
length. The artifact store refuses path traversal and verifies both hashes and sizes.

Current release-stage evidence lives under `artifacts/stages/Rx/` and `artifacts/stages/S10/` and
includes:

```text
preflight.json
stage_report.json
commands.log
test-results.xml
evidence/
external-validation.md
artifact-index.json
```

Large, historical, or private diagnostics belong under ignored local paths. Public artifacts must
be intentionally curated and redacted, must not contain credentials or private paths, and must
retain provenance links to their generating run and configuration hash. Benchmark v1 labels are
allowed only inside its explicit integration fixtures; oracle-bearing fields never enter Executor
work items.

The public v0.1 evidence surface is R2 EvalPlan onboarding, R3 paired functional evaluation, R4
evolution/Gates, R5 report, and the corresponding R1–R5/S10 stage reports. Earlier S-stage outputs
are historical development records and are not shipped as release evidence.

The `codex/graph-hardening` branch keeps the complete GH-E1 research chain isolated without
overwriting v0.1. Its public Git surface is intentionally curated:

```text
artifacts/runs/gh-e1-slack-gif-creator-report/
artifacts/runs/gh-e1-slack-gif-creator-report/final/
artifacts/stages/GH-E0.5/
artifacts/stages/GH-E1/
artifacts/stages/POST-GH-E1-CLEANUP/
artifacts/stages/POST-GH-E1-FINALIZATION/
```

The report-root, final-report, GH-E0.5, GH-E1, cleanup, and finalization seals verify 36, 32, 69,
88, 12, and 7 indexed objects respectively. The final report has 29 hash-bound task-native GIFs
and preserves the `no_strict_improvement` result. The complete reference and evolution runs (485
and 579 indexed objects) remain byte-preserved under ignored local paths. They are sealed research
evidence, not public Git payloads, because they contain raw Agent workspaces and optional
machine-local diagnostics. A clean clone therefore verifies the public report and stage seals,
not the unpublished full raw evolution seal.

Post-GH-E1 cleanup evidence is stored under `artifacts/stages/POST-GH-E1-CLEANUP/`. It is a new
read-only audit surface: it may hash and replay sealed GH-E1 inputs, but it must not be added to any
GH-E1 run/stage index or reinterpret the sealed `no_strict_improvement` outcome.

Raw Agent workspaces remain byte-preserved locally. Four optional diagnostics contain six
private-path findings and are explicitly quarantined by hash; none appears in the 138 artifacts
referenced by the 29 Core-accepted E2 records or in the published report/stage surface. Required
evidence therefore passes the safety scan without rewriting or deleting the original diagnostics.
