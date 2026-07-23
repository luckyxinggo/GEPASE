# Security Policy

## Reporting

Please report suspected vulnerabilities privately to the repository maintainers before public
disclosure. Do not include real credentials or private Skill content in an issue.

## Trust boundaries

- `skills_test/` is local-only and must remain ignored.
- Candidate packages are materialized into dedicated sibling workspaces; the optimizer never edits
  the source Package in place.
- Agent-generated evidence is untrusted input and must pass schema, path, and secret validation.
- Headless provider credentials are optional. Public configuration stores only an environment
  variable name; adapters must read the value at execution time and must never serialize it.
- Simulated evidence must never be promoted to observed evidence without provenance.
- Package scripts and Agent-produced artifacts are untrusted. Run a new Skill only in a sandbox or
  host workspace whose filesystem, network, credentials, and tool permissions match its risk.
- `report deploy` verifies the sealed report and file hashes, refuses an existing destination, and
  copies files only from the report's bounded deployable Package directory.

The Python Core does not implement a general Agent Runtime, login session, or unrestricted tool
scheduler. Agent-native and optional Headless host adapters remain separate trust boundaries.
