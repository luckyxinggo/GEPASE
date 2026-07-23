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
