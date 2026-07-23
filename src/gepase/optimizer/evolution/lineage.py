"""Deterministic ancestry queries over the immutable candidate DAG."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from gepase.optimizer.evolution.models import EvolutionCandidateIdentity


class CandidateAncestryIndex:
    """Index explicit parent edges without reconstructing or guessing missing lineage."""

    def __init__(self, candidates: Iterable[EvolutionCandidateIdentity]) -> None:
        by_id: dict[str, EvolutionCandidateIdentity] = {}
        for candidate in candidates:
            previous = by_id.get(candidate.candidate_id)
            if previous is not None and previous != candidate:
                raise ValueError("candidate_id reused with different lineage identity")
            by_id[candidate.candidate_id] = candidate
        if not by_id:
            raise ValueError("ancestry index requires at least one candidate")
        self._by_id = by_id
        self._children: dict[str, set[str]] = {candidate_id: set() for candidate_id in by_id}
        for candidate in by_id.values():
            root = by_id.get(candidate.lineage_root_candidate_id)
            if root is None:
                raise ValueError(f"missing explicit lineage root for {candidate.candidate_id}")
            if root.generation != 0:
                raise ValueError("lineage_root_candidate_id must identify generation zero")
            for parent_id in candidate.parent_ids:
                if parent_id not in by_id:
                    raise ValueError(
                        f"missing explicit lineage parent {parent_id} for {candidate.candidate_id}"
                    )
                parent = by_id[parent_id]
                if (
                    parent.package_id != candidate.package_id
                    or parent.source_package_ref != candidate.source_package_ref
                    or parent.source_snapshot_hash != candidate.source_snapshot_hash
                    or parent.lineage_root_candidate_id != candidate.lineage_root_candidate_id
                ):
                    raise ValueError("lineage edge crosses candidate family")
                if parent.generation >= candidate.generation:
                    raise ValueError("lineage parent generation must precede child")
                self._children[parent_id].add(candidate.candidate_id)
        self._assert_acyclic()
        for candidate in by_id.values():
            branch_root_id = candidate.branch_root_candidate_id
            if branch_root_id is None:
                continue
            if branch_root_id not in by_id:
                raise ValueError("missing explicit branch_root_candidate_id")
            if not self.is_ancestor(branch_root_id, candidate.candidate_id, strict=False):
                raise ValueError("branch root must be an ancestor of the candidate")
            branch_root = by_id[branch_root_id]
            if branch_root.branch_id != candidate.branch_id:
                raise ValueError("branch_id must match branch root")

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_id))

    def get(self, candidate_id: str) -> EvolutionCandidateIdentity:
        try:
            return self._by_id[candidate_id]
        except KeyError as exc:
            raise KeyError(f"unknown candidate_id: {candidate_id}") from exc

    def _assert_acyclic(self) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(candidate_id: str) -> None:
            if candidate_id in visiting:
                raise ValueError("candidate lineage contains a cycle")
            if candidate_id in visited:
                return
            visiting.add(candidate_id)
            for parent_id in self._by_id[candidate_id].parent_ids:
                visit(parent_id)
            visiting.remove(candidate_id)
            visited.add(candidate_id)

        for candidate_id in self._by_id:
            visit(candidate_id)

    def ancestors(self, candidate_id: str, *, include_self: bool = False) -> frozenset[str]:
        self.get(candidate_id)
        found = {candidate_id} if include_self else set()
        pending = list(self._by_id[candidate_id].parent_ids)
        while pending:
            current = pending.pop()
            if current in found:
                continue
            found.add(current)
            pending.extend(self._by_id[current].parent_ids)
        return frozenset(found)

    def is_ancestor(
        self,
        ancestor_candidate_id: str,
        descendant_candidate_id: str,
        *,
        strict: bool = True,
    ) -> bool:
        self.get(ancestor_candidate_id)
        self.get(descendant_candidate_id)
        if not strict and ancestor_candidate_id == descendant_candidate_id:
            return True
        return ancestor_candidate_id in self.ancestors(descendant_candidate_id)

    def _depth(self, candidate_id: str, memo: dict[str, int]) -> int:
        if candidate_id in memo:
            return memo[candidate_id]
        parents = self._by_id[candidate_id].parent_ids
        depth = 0 if not parents else 1 + max(self._depth(parent, memo) for parent in parents)
        memo[candidate_id] = depth
        return depth

    def lowest_common_ancestor(self, candidate_ids: Iterable[str]) -> str | None:
        ids = tuple(candidate_ids)
        if not ids:
            raise ValueError("lowest_common_ancestor requires candidate ids")
        common: set[str] | None = None
        for candidate_id in ids:
            lineage = set(self.ancestors(candidate_id, include_self=True))
            common = lineage if common is None else common & lineage
        if not common:
            return None
        depths: dict[str, int] = {}
        max_depth = max(self._depth(candidate_id, depths) for candidate_id in common)
        deepest = sorted(
            candidate_id
            for candidate_id in common
            if self._depth(candidate_id, depths) == max_depth
        )
        if len(deepest) != 1:
            return None
        return deepest[0]

    def path_from_ancestor(
        self,
        ancestor_candidate_id: str,
        descendant_candidate_id: str,
    ) -> tuple[str, ...] | None:
        self.get(ancestor_candidate_id)
        self.get(descendant_candidate_id)
        if ancestor_candidate_id == descendant_candidate_id:
            return (ancestor_candidate_id,)
        queue: deque[tuple[str, tuple[str, ...]]] = deque(
            [(ancestor_candidate_id, (ancestor_candidate_id,))]
        )
        paths: list[tuple[str, ...]] = []
        shortest_length: int | None = None
        while queue:
            current, path = queue.popleft()
            if shortest_length is not None and len(path) >= shortest_length:
                continue
            for child in sorted(self._children[current]):
                next_path = (*path, child)
                if child == descendant_candidate_id:
                    shortest_length = len(next_path)
                    paths.append(next_path)
                else:
                    queue.append((child, next_path))
        unique = sorted(set(paths))
        if len(unique) != 1:
            return None
        return unique[0]

    def first_divergent_child(
        self,
        lca_candidate_id: str,
        descendant_candidate_id: str,
    ) -> str | None:
        path = self.path_from_ancestor(lca_candidate_id, descendant_candidate_id)
        if path is None or len(path) < 2:
            return None
        return path[1]

    def root_to_candidate_chain(self, candidate_id: str) -> tuple[str, ...] | None:
        candidate = self.get(candidate_id)
        return self.path_from_ancestor(candidate.lineage_root_candidate_id, candidate_id)
