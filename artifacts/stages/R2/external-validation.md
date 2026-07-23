# R2 external validation

The Eval Designer was a real isolated Agent-native run, and its provenance is stored in
`artifacts/runs/r2-slack-gif-creator-evalplan/designer-submission.json`.

The review was performed as `agent-assisted`; it is not represented as user or external human
review. Core accepted all 26 explicit decisions, rechecked the plan, froze the immutable hash, and
resumed the same run.

The in-app browser security policy rejected automated direct `file://` navigation. No alternate
browser, local-server workaround, or policy bypass was attempted. The user then opened the
self-contained file locally and confirmed that the previously listed core interactions behaved
normally. That confirmation is recorded in `evidence/human-offline-validation.json` and closes the
R2 interaction Gate.

This confirmation validates the offline review interface only. It does not rewrite the 26 case
decisions as user semantic review: those decisions remain truthfully recorded as `agent-assisted`.
