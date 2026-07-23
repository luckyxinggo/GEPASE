"""Train-only breeding snapshot and immutable held-out selection lock."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from pydantic import Field, model_validator

from gepase.optimizer.evolution.models import (
    BreedingSnapshot,
    EvolutionCandidateIdentity,
    MergeParentCandidate,
)
from gepase.schemas.common import FrozenModel

_FORBIDDEN_SELECTION_MARKERS = (
    "accept",
    "deploy",
    "heldout",
    "held_out",
    "held-out",
    "validation",
    "gate_3",
    "gate3",
    "e2",
    "e3",
)


def _canonical_hash(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")  # type: ignore[union-attr]
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _assert_train_features(feature_names: tuple[str, ...]) -> None:
    if not feature_names:
        raise ValueError("selection requires explicit train-only features")
    matches = [
        name
        for name in feature_names
        if any(marker in name.casefold() for marker in _FORBIDDEN_SELECTION_MARKERS)
    ]
    if matches:
        raise ValueError(f"held-out/deployment selection features are forbidden: {matches}")


def build_breeding_snapshot(
    candidates: tuple[MergeParentCandidate, ...],
    lineage: tuple[EvolutionCandidateIdentity, ...],
    *,
    selection_config_hash: str,
    selection_feature_names: tuple[str, ...],
    train_evidence_refs: tuple[str, ...] = (),
    created_at: datetime | None = None,
) -> BreedingSnapshot:
    """Freeze a deterministic candidate set before any held-out evaluation."""

    _assert_train_features(selection_feature_names)
    if not candidates:
        raise ValueError("breeding snapshot requires train-promising candidates")
    ordered_candidates = tuple(sorted(candidates, key=lambda item: item.identity.candidate_id))
    ordered_lineage = tuple(sorted(lineage, key=lambda item: item.candidate_id))
    refs = tuple(
        sorted(
            {
                *train_evidence_refs,
                *(ref for candidate in ordered_candidates for ref in candidate.train_evidence_refs),
            }
        )
    )
    payload = {
        "selection_config_hash": selection_config_hash,
        "selection_feature_names": sorted(set(selection_feature_names)),
        "candidate_ids": [candidate.identity.candidate_id for candidate in ordered_candidates],
        "candidate_content_hashes": {
            candidate.identity.candidate_id: candidate.identity.content_hash
            for candidate in ordered_candidates
        },
        "lineage": [item.model_dump(mode="json") for item in ordered_lineage],
        "train_evidence_refs": refs,
        "held_out_fields_redacted": True,
    }
    return BreedingSnapshot(
        snapshot_id=f"breeding-snapshot-{_canonical_hash(payload)[:24]}",
        candidates=ordered_candidates,
        lineage=ordered_lineage,
        selection_config_hash=selection_config_hash,
        train_evidence_refs=refs,
        held_out_fields_redacted=True,
        created_at=created_at or datetime.now(UTC),
    )


class LockedCandidate(FrozenModel):
    candidate_id: str
    package_id: str
    branch_id: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    failure_cluster_ids: tuple[str, ...] = Field(min_length=1)


class ValidationSelectionLock(FrozenModel):
    schema_version: str = "1.0.0"
    lock_id: str
    breeding_snapshot_id: str
    selection_config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    selection_feature_names: tuple[str, ...] = Field(min_length=1)
    locked_candidates: tuple[LockedCandidate, ...] = Field(min_length=1)
    train_evidence_refs: tuple[str, ...] = Field(min_length=1)
    created_at: datetime
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def lock_invariants(self) -> ValidationSelectionLock:
        _assert_train_features(self.selection_feature_names)
        if len({item.candidate_id for item in self.locked_candidates}) != len(
            self.locked_candidates
        ):
            raise ValueError("validation lock contains duplicate candidates")
        payload = _lock_payload(
            self.breeding_snapshot_id,
            self.selection_config_hash,
            self.selection_feature_names,
            self.locked_candidates,
            self.train_evidence_refs,
        )
        if _canonical_hash(payload) != self.content_hash:
            raise ValueError("validation selection lock content hash mismatch")
        return self


def _lock_payload(
    snapshot_id: str,
    config_hash: str,
    feature_names: tuple[str, ...],
    candidates: tuple[LockedCandidate, ...],
    evidence_refs: tuple[str, ...],
) -> dict[str, object]:
    return {
        "breeding_snapshot_id": snapshot_id,
        "selection_config_hash": config_hash,
        "selection_feature_names": sorted(set(feature_names)),
        "locked_candidates": [
            item.model_dump(mode="json")
            for item in sorted(candidates, key=lambda row: row.candidate_id)
        ],
        "train_evidence_refs": sorted(set(evidence_refs)),
    }


def build_validation_selection_lock(
    snapshot: BreedingSnapshot,
    *,
    selection_feature_names: tuple[str, ...],
    minimum_candidates_per_package: int = 2,
    created_at: datetime | None = None,
) -> ValidationSelectionLock:
    _assert_train_features(selection_feature_names)
    if minimum_candidates_per_package < 2:
        raise ValueError("held-out lock requires at least two candidates per package")
    rows: list[LockedCandidate] = []
    by_package: dict[str, list[MergeParentCandidate]] = {}
    for candidate in snapshot.candidates:
        by_package.setdefault(candidate.identity.package_id, []).append(candidate)
    for package_id, candidates in sorted(by_package.items()):
        branches = {
            item.identity.branch_id for item in candidates if item.identity.branch_id is not None
        }
        if len(candidates) < minimum_candidates_per_package or len(branches) < 2:
            raise ValueError(f"{package_id} lacks two independently branched locked candidates")
        for candidate in sorted(candidates, key=lambda item: item.identity.candidate_id):
            identity = candidate.identity
            if identity.branch_id is None or not identity.failure_cluster_ids:
                raise ValueError("locked derived candidate lacks branch/cluster identity")
            rows.append(
                LockedCandidate(
                    candidate_id=identity.candidate_id,
                    package_id=identity.package_id,
                    branch_id=identity.branch_id,
                    content_hash=identity.content_hash,
                    failure_cluster_ids=identity.failure_cluster_ids,
                )
            )
    locked = tuple(rows)
    payload = _lock_payload(
        snapshot.snapshot_id,
        snapshot.selection_config_hash,
        selection_feature_names,
        locked,
        snapshot.train_evidence_refs,
    )
    content_hash = _canonical_hash(payload)
    return ValidationSelectionLock(
        lock_id=f"validation-selection-lock-{content_hash[:24]}",
        breeding_snapshot_id=snapshot.snapshot_id,
        selection_config_hash=snapshot.selection_config_hash,
        selection_feature_names=tuple(sorted(set(selection_feature_names))),
        locked_candidates=locked,
        train_evidence_refs=snapshot.train_evidence_refs,
        created_at=created_at or datetime.now(UTC),
        content_hash=content_hash,
    )


def verify_selection_lock(
    lock: ValidationSelectionLock,
    candidates: tuple[MergeParentCandidate, ...],
) -> dict[str, object]:
    expected = {item.candidate_id: item.content_hash for item in lock.locked_candidates}
    observed = {item.identity.candidate_id: item.identity.content_hash for item in candidates}
    replacement_count = sum(
        expected.get(candidate_id) != content_hash
        for candidate_id, content_hash in observed.items()
        if candidate_id in expected
    )
    missing = sorted(set(expected) - set(observed))
    added = sorted(set(observed) - set(expected))
    return {
        "schema_version": "1.0.0",
        "valid": replacement_count == 0 and not missing and not added,
        "lock_id": lock.lock_id,
        "locked_candidate_count": len(expected),
        "replacement_count": replacement_count,
        "missing_candidate_ids": missing,
        "added_candidate_ids": added,
    }


def audit_breeding_isolation(
    snapshot: BreedingSnapshot,
    lock: ValidationSelectionLock,
) -> dict[str, object]:
    feature_matches = [
        name
        for name in lock.selection_feature_names
        if any(marker in name.casefold() for marker in _FORBIDDEN_SELECTION_MARKERS)
    ]
    verification = verify_selection_lock(lock, snapshot.candidates)
    return {
        "schema_version": "1.0.0",
        "valid": not feature_matches and bool(verification["valid"]),
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_candidate_count": len(snapshot.candidates),
        "locked_candidate_count": len(lock.locked_candidates),
        "held_out_or_deployable_feature_count": len(feature_matches),
        "held_out_fields_redacted": snapshot.held_out_fields_redacted,
        "lock_after_snapshot": lock.created_at >= snapshot.created_at,
        "lock_replacement_count": verification["replacement_count"],
    }
