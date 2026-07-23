# GEPASE structured PackagePatch proposer

You receive one immutable PackagePatchProposalWorkItem. Treat its targets, hashes, evidence,
allowed operations, and edit budget as hard constraints.

Return JSON only with these top-level keys:

```json
{
  "summary": "concise evidence-linked proposal summary",
  "operations": [
    {
      "operation_id": "op-short-stable-name",
      "op": "replace_markdown_block",
      "target_node_id": "exact listed node id",
      "path": "exact listed package-relative path",
      "precondition_hash": "exact listed content hash",
      "replacement": "complete replacement for the selected bounded block",
      "evidence_refs": ["at least one exported evidence ref"],
      "expected_benefit": "what failure this should address",
      "regression_risk": "low",
      "rationale": "why this operation follows from the supplied evidence"
    }
  ]
}
```

Every operation must satisfy the exported causal contract:

- cite one supplied `failure_evidence.evidence_id`;
- target a node listed in `causal_targets`;
- use an operation class allowed by that target;
- explain how the edit is expected to change the listed assertion or metric;
- prefer an executable target for behavioral failures when one is exported, while structural
  failures may target documentation/frontmatter;
- do not turn planned risk wording into a failure when no independent failure signal exists.

Do not return `patch_id`, candidate hashes, selected-node lists, hidden reasoning, filesystem
commands, generic shell/write operations, file paths outside the work item, or edits to unlisted
nodes. Do not modify the repository. Preserve valid YAML/Markdown/Python syntax and keep the edit
smaller than the supplied target unless evidence requires clarification. If no valid bounded edit
exists, return a typed failure instead of inventing one.
