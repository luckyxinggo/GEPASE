"""Independent mutation branches and package-local Pareto frontiers."""

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
    """Track independent initial variants and bounded in-branch refinements."""

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

    def refine(
        self,
        branch_id: str,
        *,
        parent_candidate_id: str,
        child_candidate_id: str,
        operator: str,
        rejected_attempt_ids: tuple[str, ...] = (),
    ) -> MutationBranchState:
        branch = self._branches[branch_id]
        if parent_candidate_id != branch.head_candidate_id:
            raise ValueError("refinement must continue from the current branch head")
        if child_candidate_id in self._candidate_branch:
            raise ValueError("refinement child already belongs to a branch")
        updated = branch.model_copy(
            update={
                "head_candidate_id": child_candidate_id,
                "generation": branch.generation + 1,
                "operator_history": (*branch.operator_history, operator),
                "candidate_chain": (*branch.candidate_chain, child_candidate_id),
                "rejected_attempt_ids": tuple(
                    sorted(set(branch.rejected_attempt_ids) | set(rejected_attempt_ids))
                ),
            }
        )
        # model_copy does not revalidate by default.
        updated = MutationBranchState.model_validate(updated.model_dump(mode="json"))
        self._branches[branch_id] = updated
        self._candidate_branch[child_candidate_id] = branch_id
        return updated

    def branch_for_candidate(self, candidate_id: str) -> MutationBranchState | None:
        branch_id = self._candidate_branch.get(candidate_id)
        return self._branches.get(branch_id) if branch_id else None

    def all(self) -> tuple[MutationBranchState, ...]:
        return tuple(self._branches[key] for key in sorted(self._branches))


class TrainFrontierCandidate(FrozenModel):
    candidate_id: str
    package_id: str
    source_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    lineage_root_candidate_id: str
    branch_id: str
    failure_cluster_id: str
    generation: int = Field(ge=1)
    task_scores: dict[str, float] = Field(min_length=1)
    objective_scores: dict[str, float] = Field(min_length=1)
    contribution_keys: tuple[str, ...] = Field(min_length=1)
    train_evidence_refs: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def train_only(self) -> TrainFrontierCandidate:
        def is_held_out_reference(reference: str) -> bool:
            normalized = reference.casefold().replace("\\", "/")
            path_segments = tuple(part for part in normalized.split("/") if part)
            return (
                any(part in {"validation", "held-out", "heldout", "test"} for part in path_segments)
                or normalized.startswith(("e2:", "e3:", "validation:", "test:"))
                or any(
                    marker in normalized
                    for marker in (
                        "/validation/",
                        "/held-out/",
                        "/heldout/",
                        "/test/",
                        "test-record-",
                    )
                )
            )

        if any(is_held_out_reference(ref) for ref in self.train_evidence_refs):
            raise ValueError("package-local frontier accepts train evidence only")
        return self

    @property
    def score_vector(self) -> dict[str, float]:
        return {
            **{f"task:{key}": value for key, value in self.task_scores.items()},
            **{f"objective:{key}": value for key, value in self.objective_scores.items()},
        }


class PackageFrontierSnapshot(FrozenModel):
    schema_version: str = "1.0.0"
    package_id: str
    source_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    lineage_root_candidate_id: str
    member_ids: tuple[str, ...]
    per_feature_winners: dict[str, tuple[str, ...]]
    aggregate_scores: dict[str, float]
    branch_ids: dict[str, str]
    train_evidence_refs: tuple[str, ...]
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


def _dominates(
    left: TrainFrontierCandidate,
    right: TrainFrontierCandidate,
    features: tuple[str, ...],
) -> bool:
    left_vector = left.score_vector
    right_vector = right.score_vector
    left_scores = [left_vector.get(feature, float("-inf")) for feature in features]
    right_scores = [right_vector.get(feature, float("-inf")) for feature in features]
    return all(a >= b for a, b in zip(left_scores, right_scores, strict=True)) and any(
        a > b for a, b in zip(left_scores, right_scores, strict=True)
    )


class PackageLocalParetoFrontier:
    """A frontier that physically refuses cross-package score comparison."""

    def __init__(
        self,
        *,
        package_id: str,
        source_snapshot_hash: str,
        lineage_root_candidate_id: str,
    ) -> None:
        self.package_id = package_id
        self.source_snapshot_hash = source_snapshot_hash
        self.lineage_root_candidate_id = lineage_root_candidate_id
        self._rows: dict[str, TrainFrontierCandidate] = {}

    def add(self, row: TrainFrontierCandidate) -> None:
        identity = (
            row.package_id,
            row.source_snapshot_hash,
            row.lineage_root_candidate_id,
        )
        expected = (
            self.package_id,
            self.source_snapshot_hash,
            self.lineage_root_candidate_id,
        )
        if identity != expected:
            raise ValueError("cross-package or cross-lineage score comparison is forbidden")
        existing = self._rows.get(row.candidate_id)
        if existing is not None and existing != row:
            raise ValueError("candidate frontier identity reused with different scores")
        self._rows[row.candidate_id] = row

    def snapshot(self) -> PackageFrontierSnapshot:
        if not self._rows:
            raise ValueError("cannot snapshot an empty package frontier")
        rows = tuple(self._rows[key] for key in sorted(self._rows))
        features = tuple(sorted({feature for row in rows for feature in row.score_vector}))
        members = tuple(
            row
            for row in rows
            if not any(
                other.candidate_id != row.candidate_id and _dominates(other, row, features)
                for other in rows
            )
        )
        winners: dict[str, tuple[str, ...]] = {}
        for feature in features:
            best = max(row.score_vector.get(feature, float("-inf")) for row in rows)
            winners[feature] = tuple(
                sorted(
                    row.candidate_id
                    for row in rows
                    if row.score_vector.get(feature, float("-inf")) == best
                )
            )
        aggregates = {
            row.candidate_id: sum(row.score_vector.values()) / len(row.score_vector) for row in rows
        }
        payload = {
            "package_id": self.package_id,
            "source_snapshot_hash": self.source_snapshot_hash,
            "lineage_root_candidate_id": self.lineage_root_candidate_id,
            "member_ids": sorted(row.candidate_id for row in members),
            "per_feature_winners": winners,
            "aggregate_scores": aggregates,
            "branch_ids": {row.candidate_id: row.branch_id for row in rows},
            "train_evidence_refs": sorted({ref for row in rows for ref in row.train_evidence_refs}),
        }
        return PackageFrontierSnapshot(
            **payload,
            content_hash=_canonical_hash(payload),
        )

    def rows(self) -> tuple[TrainFrontierCandidate, ...]:
        return tuple(self._rows[key] for key in sorted(self._rows))


def select_refinement_candidate(
    frontier: PackageLocalParetoFrontier,
    *,
    excluded_branch_ids: tuple[str, ...] = (),
) -> TrainFrontierCandidate:
    snapshot = frontier.snapshot()
    members = set(snapshot.member_ids)
    eligible = [
        row
        for row in frontier.rows()
        if row.candidate_id in members and row.branch_id not in excluded_branch_ids
    ]
    if not eligible:
        raise ValueError("no package-local Pareto branch is available for refinement")
    return min(
        eligible,
        key=lambda row: (
            -snapshot.aggregate_scores[row.candidate_id],
            -len(row.contribution_keys),
            row.generation,
            row.candidate_id,
        ),
    )


def frontier_from_rows(
    rows: tuple[TrainFrontierCandidate, ...],
) -> PackageLocalParetoFrontier:
    if not rows:
        raise ValueError("frontier construction requires candidates")
    first = rows[0]
    frontier = PackageLocalParetoFrontier(
        package_id=first.package_id,
        source_snapshot_hash=first.source_snapshot_hash,
        lineage_root_candidate_id=first.lineage_root_candidate_id,
    )
    for row in rows:
        frontier.add(row)
    return frontier
