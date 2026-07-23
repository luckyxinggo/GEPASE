---
name: policy-evidence-evaluator-human-v1
description: Evaluate policy evidence with explicit provenance, tier boundaries, and auditable decisions.
---

# Policy evidence evaluator

Classify each claim by its actual evidence: static fact, simulated plan, observed Agent behavior, or executable assertion. Never convert planned behavior into observed success.

## Procedure

1. Extract the policy question, evidence items, source identifiers, and requested decision format.
2. For every finding, record the evidence tier, provenance, relevant rule, and uncertainty.
3. Mark contradictions and missing evidence explicitly; absence of a trace is not proof of compliance.
4. Produce the requested artifact with a decision, evidence table, limitations, and follow-up checks.
5. Validate that every conclusion points to evidence and that no E1 plan is described as an E2/E3 observation.
