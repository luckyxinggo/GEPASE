"""E2 Agent-native delegated execution provider."""

from __future__ import annotations

from gepase.evals.errors import InvalidSubmission
from gepase.evals.evidence import EvaluationRecord
from gepase.evals.providers.base import ProviderCapabilities
from gepase.evals.providers.common import delegated_record
from gepase.evals.schema import EvidenceTier
from gepase.evals.work_items import EvalWorkItem, PackageAccessKind, WorkSubmission


class DelegatedProvider:
    capabilities = ProviderCapabilities(
        provider_id="agent-delegated-v1",
        evidence_tiers=(EvidenceTier.E2_DELEGATED,),
        capabilities=("*",),
        requires_agent_host=True,
    )

    def validate_submission(self, item: EvalWorkItem, submission: WorkSubmission) -> None:
        if item.evidence_tier is not EvidenceTier.E2_DELEGATED:
            raise InvalidSubmission("DelegatedProvider only accepts E2 work")
        if not submission.observed_trace:
            raise InvalidSubmission("E2 submission requires an observed trace")
        if not submission.artifacts or not submission.artifact_root:
            raise InvalidSubmission("E2 submission requires artifact evidence")
        if not submission.usage.nonempty:
            raise InvalidSubmission("E2 submission requires non-empty usage")
        if item.frozen_plan_hash is not None:
            if not submission.context_id:
                raise InvalidSubmission("frozen Functional E2 requires an isolated context_id")
            if submission.transcript is None:
                raise InvalidSubmission("frozen Functional E2 requires a transcript artifact")
            if item.variant == "no-skill" and submission.package_access:
                raise InvalidSubmission("no-skill execution cannot claim Package access")
            if item.variant != "no-skill":
                read_paths = {
                    event.path
                    for event in submission.package_access
                    if event.kind is PackageAccessKind.READ
                }
                executed_paths = {
                    event.path
                    for event in submission.package_access
                    if event.kind is PackageAccessKind.EXECUTED
                }
                if "SKILL.md" not in read_paths:
                    raise InvalidSubmission("with-skill execution must record reading SKILL.md")
                if not executed_paths:
                    raise InvalidSubmission(
                        "with-skill execution must record executed Package code"
                    )
                sequences = [event.sequence for event in submission.package_access]
                if sequences != sorted(set(sequences)):
                    raise InvalidSubmission("Package access sequence must be unique and ordered")
                for event in submission.package_access:
                    expected_node = item.package_node_map.get(event.path)
                    if expected_node is None or event.node_id != expected_node:
                        raise InvalidSubmission(
                            f"Package access cannot be mapped to frozen graph: {event.path}"
                        )
                    if (
                        event.kind is PackageAccessKind.READ
                        and (event.bytes_loaded == 0 or event.tokens_loaded == 0)
                    ):
                        raise InvalidSubmission(
                            f"Package read lacks byte/token accounting: {event.path}"
                        )
                if not any(path.endswith(".py") for path in executed_paths):
                    raise InvalidSubmission("with-skill execution must execute Package Python code")

    def normalize_evidence(
        self, item: EvalWorkItem, submission: WorkSubmission
    ) -> EvaluationRecord:
        self.validate_submission(item, submission)
        return delegated_record(item, submission)
