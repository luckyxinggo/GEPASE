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
