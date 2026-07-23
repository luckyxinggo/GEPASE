"""Deterministic checks for Agent-authored EvalPlan drafts."""

# ruff: noqa: RUF001 -- Chinese user-facing diagnostics use Chinese punctuation.

from __future__ import annotations

import hashlib
import re
import stat
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

from gepase.evals.eval_plan import (
    EvalDesignBrief,
    EvalPlanCheck,
    EvalPlanCheckReport,
    EvalPlanCheckStatus,
    EvalPlanDraft,
    SourceProvenance,
    TriggerCaseKind,
    UpstreamTreeManifest,
)
from gepase.evals.work_items import canonical_hash

_ORACLE_KEYS = {
    "assertions",
    "candidate_identity",
    "expected_answer",
    "expected_output_zh",
    "expected_winner",
    "expectations",
    "rubric",
    "sibling_output",
}
_LEAKAGE_TERMS = re.compile(
    r"\b(?:baseline|candidate(?:_id| identity)?|expected winner|with[- ]skill|"
    r"assertion id|rubric score)\b",
    re.IGNORECASE,
)


def _check(
    check_id: str,
    passed: bool,
    detail_zh: str,
    related: tuple[str, ...] = (),
    *,
    warning: bool = False,
) -> EvalPlanCheck:
    status = (
        EvalPlanCheckStatus.WARNING
        if warning and not passed
        else EvalPlanCheckStatus.PASSED
        if passed
        else EvalPlanCheckStatus.FAILED
    )
    return EvalPlanCheck(
        check_id=check_id,
        status=status,
        detail_zh=detail_zh,
        related_case_ids=related,
    )


def _normalized_prompt(value: str) -> str:
    return " ".join(re.sub(r"[^\w\u4e00-\u9fff]+", " ", value.casefold()).split())


def verify_upstream_tree_manifest(
    repo_root: Path,
    provenance: SourceProvenance,
) -> tuple[bool, tuple[str, ...]]:
    """Verify vendored bytes against a pinned Git tree manifest without network access."""
    manifest_path = (repo_root / provenance.upstream_manifest_ref).resolve()
    package_root = (repo_root / provenance.vendored_ref).resolve()
    if (
        not manifest_path.is_relative_to(repo_root)
        or not package_root.is_relative_to(repo_root)
        or not manifest_path.is_file()
        or not package_root.is_dir()
    ):
        return False, ("manifest-or-package-missing",)
    manifest = UpstreamTreeManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    problems: list[str] = []
    if manifest.repository_url != provenance.repository_url:
        problems.append("repository-url")
    if manifest.source_commit != provenance.source_commit:
        problems.append("source-commit")
    if manifest.source_subpath != provenance.source_subpath:
        problems.append("source-subpath")
    if manifest.upstream_tree_hash != provenance.upstream_tree_hash:
        problems.append("tree-hash")
    expected = {entry.path: entry for entry in manifest.entries}
    actual_paths = {
        path.relative_to(package_root).as_posix()
        for path in package_root.rglob("*")
        if path.is_file()
    }
    if actual_paths != set(expected):
        problems.append("file-set")
    for relative, entry in expected.items():
        path = package_root / relative
        if not path.is_file():
            continue
        data = path.read_bytes()
        header = f"blob {len(data)}\0".encode()
        digest = hashlib.sha1(header + data, usedforsecurity=False).hexdigest()
        if digest != entry.git_blob_sha1:
            problems.append(f"blob:{relative}")
        actual_mode = "100755" if path.stat().st_mode & stat.S_IXUSR else "100644"
        if actual_mode != entry.mode:
            problems.append(f"mode:{relative}")
    return not problems, tuple(problems)


def run_eval_plan_checks(
    repo_root: Path,
    draft: EvalPlanDraft,
    provenance: SourceProvenance,
    brief: EvalDesignBrief,
) -> EvalPlanCheckReport:
    checks: list[EvalPlanCheck] = []
    trigger_ids = [case.case_id for case in draft.trigger_cases]
    functional_ids = [case.case_id for case in draft.functional_cases]
    all_ids = trigger_ids + functional_ids
    checks.append(
        _check(
            "schema.unique-case-id",
            len(all_ids) == len(set(all_ids)),
            "所有 trigger/functional case ID 全局唯一。",
            tuple(case_id for case_id, count in Counter(all_ids).items() if count > 1),
        )
    )

    trigger_counts = Counter(case.kind for case in draft.trigger_cases)
    missing_trigger_kinds = tuple(
        kind.value
        for kind in TriggerCaseKind
        if trigger_counts[kind] < brief.minimum_trigger_cases_per_kind
    )
    checks.append(
        _check(
            "trigger.coverage",
            not missing_trigger_kinds,
            "正例、负例和近边界 trigger query 均达到设计简报的最小数量。",
            missing_trigger_kinds,
        )
    )

    families = {case.case_family for case in draft.functional_cases}
    missing_families = tuple(sorted(set(brief.required_functional_families) - families))
    train_count = sum(case.split == "train" for case in draft.functional_cases)
    validation_count = sum(case.split == "validation" for case in draft.functional_cases)
    functional_count_ok = (
        len(draft.functional_cases) >= brief.minimum_functional_cases
        and train_count >= brief.minimum_train_cases
        and validation_count >= brief.minimum_validation_cases
    )
    checks.append(
        _check(
            "functional.coverage",
            functional_count_ok and not missing_families,
            "功能 case 数量、train/validation 分布和必需 family 覆盖满足设计简报。",
            missing_families,
        )
    )

    unsupported_media = tuple(
        case.case_id
        for case in draft.functional_cases
        if case.requested_output.media_type not in brief.required_output_media_types
    )
    checks.append(
        _check(
            "functional.native-output",
            not unsupported_media,
            "每个功能 case 都声明了任务原生输出，且媒体类型在设计简报允许范围内。",
            unsupported_media,
        )
    )

    missing_or_changed: list[str] = []
    license_errors: list[str] = []
    fixture_hashes_by_split: defaultdict[str, set[str]] = defaultdict(set)
    fixture_ids_by_hash: defaultdict[str, list[str]] = defaultdict(list)
    for case in draft.functional_cases:
        for fixture in case.fixtures:
            path = (repo_root / fixture.ref).resolve()
            if not path.is_relative_to(repo_root) or not path.is_file():
                missing_or_changed.append(case.case_id)
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != fixture.sha256:
                missing_or_changed.append(case.case_id)
            if not fixture.license.strip():
                license_errors.append(case.case_id)
            fixture_hashes_by_split[case.split].add(fixture.sha256)
            fixture_ids_by_hash[fixture.sha256].append(case.case_id)
    checks.append(
        _check(
            "fixture.integrity",
            not missing_or_changed,
            "fixture 存在、位于仓库内且内容 hash 与 draft 一致。",
            tuple(missing_or_changed),
        )
    )
    checks.append(
        _check(
            "fixture.license",
            not license_errors,
            "所有 fixture 都有明确许可声明。",
            tuple(license_errors),
        )
    )

    duplicate_fixture_ids = tuple(
        case_id
        for case_ids in fixture_ids_by_hash.values()
        if len(case_ids) > 1
        for case_id in case_ids
    )
    cross_split_hashes = fixture_hashes_by_split["train"] & fixture_hashes_by_split["validation"]
    cross_split_fixture_ids = tuple(
        case_id for digest in cross_split_hashes for case_id in fixture_ids_by_hash[digest]
    )
    checks.append(
        _check(
            "split.fixture-leakage",
            not duplicate_fixture_ids and not cross_split_fixture_ids,
            "train/validation 不复用相同 fixture 内容，所有功能 case 的 fixture 独立。",
            tuple(sorted(set((*duplicate_fixture_ids, *cross_split_fixture_ids)))),
        )
    )

    split_by_leakage_group: defaultdict[str, set[str]] = defaultdict(set)
    for case in draft.functional_cases:
        split_by_leakage_group[case.leakage_group].add(case.split)
    leaking_groups = tuple(
        sorted(group for group, splits in split_by_leakage_group.items() if len(splits) > 1)
    )
    checks.append(
        _check(
            "split.group-leakage",
            not leaking_groups,
            "同一 leakage group 未跨越 train/validation。",
            leaking_groups,
        )
    )

    near_pairs: list[str] = []
    for index, left in enumerate(draft.functional_cases):
        for right in draft.functional_cases[index + 1 :]:
            if left.split == right.split:
                continue
            ratio = SequenceMatcher(
                None,
                _normalized_prompt(left.prompt),
                _normalized_prompt(right.prompt),
            ).ratio()
            if ratio >= 0.86:
                near_pairs.extend((left.case_id, right.case_id))
    checks.append(
        _check(
            "split.near-duplicate",
            not near_pairs,
            "跨 split prompt 未达到近重复阈值 0.86。",
            tuple(sorted(set(near_pairs))),
        )
    )

    weak_cases = tuple(
        case.case_id
        for case in draft.functional_cases
        if not any(
            item.deterministic and item.evidence_kind != "file_presence"
            for item in case.expectations
        )
    )
    checks.append(
        _check(
            "oracle.nontrivial-assertion",
            not weak_cases,
            "每个功能 case 至少有一个超越文件存在性的内容/元数据确定性检查。",
            weak_cases,
        )
    )

    leaked_executor_views: list[str] = []
    for case in draft.functional_cases:
        view = case.executor_view()
        if _ORACLE_KEYS & set(view) or _LEAKAGE_TERMS.search(str(view)):
            leaked_executor_views.append(case.case_id)
    checks.append(
        _check(
            "isolation.executor-view",
            not leaked_executor_views,
            "Executor view 不含 assertions、rubric、expected answer、candidate 或 sibling output。",
            tuple(leaked_executor_views),
        )
    )

    e1_enabled = tuple(
        case.case_id for case in draft.functional_cases if case.evidence_policy.enable_e1
    )
    checks.append(
        _check(
            "evidence.e0-e2-e3-default",
            not e1_enabled,
            "首个 canary 的功能 case 默认使用 E2/E3，E1 未启用。",
            e1_enabled,
        )
    )

    tree_valid, tree_problems = verify_upstream_tree_manifest(repo_root, provenance)
    checks.append(
        _check(
            "source.license-and-snapshot",
            provenance.license_spdx == "Apache-2.0"
            and provenance.package_snapshot_hash == draft.package_snapshot_hash
            and tree_valid,
            "Apache-2.0 来源、逐文件 Git blob、upstream tree 与 draft PackageSnapshot 一致。",
            tree_problems,
        )
    )

    hard_failures = sum(item.status is EvalPlanCheckStatus.FAILED for item in checks)
    warnings = sum(item.status is EvalPlanCheckStatus.WARNING for item in checks)
    return EvalPlanCheckReport(
        draft_hash=canonical_hash(draft),
        valid=hard_failures == 0,
        checks=tuple(checks),
        metrics={
            "trigger_cases": len(draft.trigger_cases),
            "functional_cases": len(draft.functional_cases),
            "train_functional_cases": train_count,
            "validation_functional_cases": validation_count,
            "functional_families": len(families),
            "hard_failures": hard_failures,
            "warnings": warnings,
            "trigger_functional_channels_separate": True,
        },
    )
