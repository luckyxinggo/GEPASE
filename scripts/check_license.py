"""Release license and public-fixture attribution policy check."""

import json
from pathlib import Path


def main() -> int:
    root = Path.cwd()
    license_text = (root / "LICENSE").read_text(encoding="utf-8")
    project_text = (root / "pyproject.toml").read_text(encoding="utf-8")
    errors: list[str] = []
    if "Apache License" not in license_text or 'license = { file = "LICENSE" }' not in project_text:
        errors.append("project Apache-2.0 declaration is incomplete")
    for package in sorted((root / "benchmarks/skills").iterdir()):
        if not package.is_dir():
            continue
        provenance = package / "provenance.json"
        license_path = package / "LICENSE"
        if not provenance.is_file() or not license_path.is_file():
            errors.append(f"{package.relative_to(root)} is missing license/provenance")
            continue
        payload = json.loads(provenance.read_text(encoding="utf-8"))
        if (
            payload.get("license") != "Apache-2.0"
            or payload.get("private_content_copied") is not False
        ):
            errors.append(f"{provenance.relative_to(root)} has invalid attribution")
    canary = root / "benchmarks/canaries/slack-gif-creator/source-provenance.json"
    canary_payload = json.loads(canary.read_text(encoding="utf-8"))
    license_ref = root / str(canary_payload.get("license_ref", ""))
    if canary_payload.get("license_spdx") != "Apache-2.0" or not license_ref.is_file():
        errors.append("slack-gif-creator license attribution is incomplete")
    report_license = (
        root / "artifacts/runs/r5-slack-gif-creator-report/deployable/package/LICENSE.txt"
    )
    if not report_license.is_file() or report_license.read_bytes() != license_ref.read_bytes():
        errors.append("deployable report Package does not retain the canary license")
    valid = not errors
    print("license policy: ok" if valid else "license policy: failed\n" + "\n".join(errors))
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
