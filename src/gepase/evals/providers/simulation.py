"""E1 simulated rollout provider and prompt contract."""

from __future__ import annotations

from gepase.evals.errors import InvalidSubmission
from gepase.evals.evidence import EvaluationRecord
from gepase.evals.providers.base import ProviderCapabilities
from gepase.evals.providers.common import delegated_record
from gepase.evals.schema import EvidenceTier
from gepase.evals.work_items import EvalWorkItem, WorkSubmission

SIMULATION_PROMPT = """Plan the task without executing tools. Return selected package nodes,
ordered steps, intended tools, risks, and uncertainty. Never claim that a file, command, service,
or assertion succeeded. Do not infer or reveal hidden expected outputs."""


class SimulationProvider:
    capabilities = ProviderCapabilities(
        provider_id="agent-simulation-v1",
        evidence_tiers=(EvidenceTier.E1_SIMULATED,),
        capabilities=("plan_task",),
        requires_agent_host=True,
    )

    def validate_submission(self, item: EvalWorkItem, submission: WorkSubmission) -> None:
        if item.evidence_tier is not EvidenceTier.E1_SIMULATED:
            raise InvalidSubmission("SimulationProvider only accepts E1 work")
        if submission.observed_trace or submission.artifacts:
            raise InvalidSubmission("E1 submission cannot contain observed trace or artifacts")
        if not submission.planned_trace:
            raise InvalidSubmission("E1 submission requires a planned trace")
        if submission.proxy_score is not None and not submission.proxy_score_method:
            raise InvalidSubmission("E1 proxy score requires a named scoring method")

    def normalize_evidence(
        self, item: EvalWorkItem, submission: WorkSubmission
    ) -> EvaluationRecord:
        self.validate_submission(item, submission)
        return delegated_record(item, submission)
