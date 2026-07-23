# slack-gif-creator R2 canary

This directory vendors the complete Apache-2.0 `slack-gif-creator` Skill Package from
`anthropics/skills` at commit `fa0fa64bdc967915dc8399e803be67759e1e62b8`.

- `package/` is an exact byte-for-byte upstream snapshot and remains read-only during evaluation.
- `source-provenance.json` binds the upstream commit/tree, GEPASE PackageSnapshot hash, license,
  dependency lock, and `upstream-tree.json`; the latter records every upstream Git blob so vendored
  bytes can be verified offline without trusting a declared tree hash alone.
- `design-brief.json` and `fixtures/` are Package-specific EvalPlan inputs. They are not built into
  the generic Eval Engine.
- `requirements.lock` is the R2 local execution resolution; it does not modify the vendored package.

R2 only establishes the onboarding/review/freeze workflow and local technical smoke. Functional
no-skill/original execution and quality scoring begin in R3.
