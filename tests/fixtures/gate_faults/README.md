# Gate fault fixtures

This directory anchors the pre-registered S7 fault families exercised by
`gepase gate fault-suite`: schema/path escape, stale precondition, Python syntax,
broken reference, and unsafe script behavior. The command materializes faults in
run-local temporary workspaces and never edits benchmark packages in place.
