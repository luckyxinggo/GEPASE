"""Independent first-generation mutation branch identity."""

from __future__ import annotations

import hashlib
import json

from pydantic import Field, model_validator

from gepase.schemas.common import FrozenModel


def _canonical_hash(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")  # type: ignore[union-attr]
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


class FrozenLineageRoot(FrozenModel):
    package_id: str
    source_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    root_candidate_id: str
    root_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class MutationBranchState(FrozenModel):
    branch_id: str
    package_id: str
    source_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    lineage_root_candidate_id: str
    branch_root_candidate_id: str
    failure_cluster_id: str
    variant_index: int = Field(ge=0)
    head_candidate_id: str
    generation: int = Field(ge=1)
    operator_history: tuple[str, ...] = Field(min_length=1)
    candidate_chain: tuple[str, ...] = Field(min_length=2)
    rejected_attempt_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def chain_contract(self) -> MutationBranchState:
        if self.candidate_chain[0] != self.lineage_root_candidate_id:
            raise ValueError("branch chain must begin at the frozen lineage root")
        if self.candidate_chain[1] != self.branch_root_candidate_id:
            raise ValueError("branch root must be the first divergent child")
        if self.candidate_chain[-1] != self.head_candidate_id:
            raise ValueError("branch head differs from candidate chain")
        if len(self.operator_history) != self.generation:
            raise ValueError("operator history must account for every branch generation")
        if len(self.candidate_chain) != self.generation + 1:
            raise ValueError("candidate chain length is inconsistent with generation")
        return self


def freeze_lineage_root(
    *,
    package_id: str,
    source_snapshot_hash: str,
    root_candidate_id: str,
    root_content_hash: str,
    config_hash: str,
) -> FrozenLineageRoot:
    return FrozenLineageRoot(
        package_id=package_id,
        source_snapshot_hash=source_snapshot_hash,
        root_candidate_id=root_candidate_id,
        root_content_hash=root_content_hash,
        config_hash=config_hash,
    )


class BranchRegistry:
    """Track the independent first-generation branches used by the controller."""

    def __init__(self, root: FrozenLineageRoot) -> None:
        self.root = root
        self._branches: dict[str, MutationBranchState] = {}
        self._candidate_branch: dict[str, str] = {}

    def create_initial(
        self,
        *,
        failure_cluster_id: str,
        variant_index: int,
        child_candidate_id: str,
        operator: str,
    ) -> MutationBranchState:
        if child_candidate_id == self.root.root_candidate_id:
            raise ValueError("initial branch child must differ from the root")
        if child_candidate_id in self._candidate_branch:
            raise ValueError("candidate already belongs to a branch")
        identity = {
            "package": self.root.package_id,
            "snapshot": self.root.source_snapshot_hash,
            "root": self.root.root_candidate_id,
            "cluster": failure_cluster_id,
            "variant": variant_index,
        }
        branch_id = f"branch-{_canonical_hash(identity)[:24]}"
        if branch_id in self._branches:
            raise ValueError("initial variant branch already exists")
        branch = MutationBranchState(
            branch_id=branch_id,
            package_id=self.root.package_id,
            source_snapshot_hash=self.root.source_snapshot_hash,
            lineage_root_candidate_id=self.root.root_candidate_id,
            branch_root_candidate_id=child_candidate_id,
            failure_cluster_id=failure_cluster_id,
            variant_index=variant_index,
            head_candidate_id=child_candidate_id,
            generation=1,
            operator_history=(operator,),
            candidate_chain=(self.root.root_candidate_id, child_candidate_id),
        )
        self._branches[branch_id] = branch
        self._candidate_branch[child_candidate_id] = branch_id
        return branch

    def branch_for_candidate(self, candidate_id: str) -> MutationBranchState | None:
        branch_id = self._candidate_branch.get(candidate_id)
        return self._branches.get(branch_id) if branch_id else None

    def all(self) -> tuple[MutationBranchState, ...]:
        return tuple(self._branches[key] for key in sorted(self._branches))
