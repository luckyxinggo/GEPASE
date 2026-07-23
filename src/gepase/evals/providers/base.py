"""Provider protocol; providers never own candidates, frontiers, or acceptance gates."""

from __future__ import annotations

from typing import Protocol

from gepase.evals.errors import UnsupportedCapability
from gepase.evals.evidence import EvaluationRecord
from gepase.evals.schema import EvidenceTier
from gepase.evals.work_items import EvalWorkItem, WorkSubmission
from gepase.schemas.common import FrozenModel


class ProviderCapabilities(FrozenModel):
    provider_id: str
    evidence_tiers: tuple[EvidenceTier, ...]
    capabilities: tuple[str, ...]
    requires_agent_host: bool = False
    requires_external_service: bool = False

    def supports(self, item: EvalWorkItem) -> bool:
        capabilities = set(self.capabilities)
        return item.evidence_tier in self.evidence_tiers and (
            "*" in capabilities or set(item.required_capabilities) <= capabilities
        )


class EvidenceProvider(Protocol):
    @property
    def capabilities(self) -> ProviderCapabilities: ...

    def validate_submission(
        self,
        item: EvalWorkItem,
        submission: WorkSubmission,
    ) -> None: ...

    def normalize_evidence(
        self,
        item: EvalWorkItem,
        submission: WorkSubmission,
    ) -> EvaluationRecord: ...


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, EvidenceProvider] = {}

    def register(self, provider: EvidenceProvider) -> None:
        provider_id = provider.capabilities.provider_id
        if provider_id in self._providers:
            raise ValueError(f"provider already registered: {provider_id}")
        self._providers[provider_id] = provider

    def get(self, provider_id: str, item: EvalWorkItem | None = None) -> EvidenceProvider:
        try:
            provider = self._providers[provider_id]
        except KeyError as error:
            raise UnsupportedCapability(f"unknown provider: {provider_id}") from error
        if item is not None and not provider.capabilities.supports(item):
            raise UnsupportedCapability(
                f"provider {provider_id} does not support {item.evidence_tier} "
                f"with {item.required_capabilities}"
            )
        return provider

    def identifiers(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))
