# Configuration

GEPASE configuration is a strict Pydantic model. Unknown fields are rejected. YAML values can
refer to a complete environment variable using `${VARIABLE_NAME}`; secret-bearing resolved fields
are redacted before hashing or serialization.

Configuration separates provider, role routing, evaluation policy, dataset, optimizer, budget, and
report settings. Agent-native execution does not require a provider API key. Optional Headless
providers declare only the environment-variable name that holds a credential; a Headless entry
must also declare its model and endpoint.

The public role names are `eval_designer`, `executor`, `independent_grader`, `comparator`,
`analyzer`, `reflection`, and `proposer`. A default provider can be overridden per role:

```bash
uv run gepase config validate configs/examples/headless-roles.yaml --format json
```

This validates and hashes the provider-neutral routing contract. v0.1 does not include a built-in
OpenAI-compatible execution client: the Agent/Headless host adapter consumes the routing while
preserving the same isolated WorkItem and submission schemas.

The generated JSON schema is [project_config.schema.json](../schemas/project_config.schema.json).

## Evolution and report defaults

R4 evolution and multi-outcome report configs use an explicit schema version so a new default can
be introduced without changing the meaning or fingerprint of sealed historical files.

| Config version | Efficiency default | Report presentation default |
|---|---|---|
| `1.0.0` | `v1_legacy` | `classic` |
| `2.0.0` | `relative_v2` | `narrative_v1` |

For a new run, start from [r4-evolution-v2.json](../configs/examples/r4-evolution-v2.json). The
omitted `efficiency_policy_mode` resolves to `relative_v2`, and the complete resolved
`RelativeEfficiencyPolicy`—including policy version, hash, comparable axes, and
`max_relative_cost_ratio`—is included in `resolved-config.json`, the config hash, runtime
checkpoints, and report provenance. Validation derives candidate-vs-original resource evidence and
passes it through the existing `ValidationGatedAcceptance` path; it does not use a second scoring or
Gate implementation.

To request historical behavior in a new `2.0.0` run, set:

```json
{
  "schema_version": "2.0.0",
  "efficiency_policy_mode": "v1_legacy",
  "relative_efficiency_policy": null
}
```

Likewise, [evolution-report-v2.json](../configs/examples/evolution-report-v2.json) omits
`presentation_mode` and therefore resolves to `narrative_v1`. Set
`"presentation_mode": "classic"` explicitly when the compact legacy renderer is required. A
sealed `1.0.0` config that omitted either new field continues to resolve as v1/classic; GEPASE
excludes those absent compatibility fields from its historical config fingerprint.

The corresponding generated schemas are
[r4_evolution_config.schema.json](../schemas/r4_evolution_config.schema.json) and
[evolution_outcome_report_config.schema.json](../schemas/evolution_outcome_report_config.schema.json).
