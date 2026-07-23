---
name: structured-report-builder-human-v1
description: Build a self-contained accessible HTML report while preserving fixture values exactly.
---

# Structured report builder

Read the named fixture before writing output. Treat titles, metric labels, metric values, and regional-detail strings as immutable evidence. Build the requested standalone `report.html` with one `h1`, a semantic table, and a visible source footer.

## Procedure

1. Parse the fixture and enumerate every required literal before authoring HTML.
2. Escape untrusted text while leaving numeric values exact.
3. Use semantic HTML and local CSS only; do not load remote assets.
4. Include `<footer>Source:` and the fixture path.
5. Re-open the final file and verify every enumerated literal, `<h1>`, `<table`, the footer, and a useful file size.

If any check fails, revise the artifact before reporting completion. Never claim a check was run when only planning it.
