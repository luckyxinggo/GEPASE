"""Submission-level validation shared by interactive and headless providers."""

from __future__ import annotations

from gepase.evals.errors import InvalidSubmission
from gepase.evals.redaction import ensure_redacted
from gepase.evals.work_items import EvalWorkItem, WorkSubmission


def validate_submission_contract(item: EvalWorkItem, submission: WorkSubmission) -> None:
    if item.work_id != submission.work_id:
        raise InvalidSubmission("submission work_id differs from the selected work item")
    if item.provider_id != submission.provider_id:
        raise InvalidSubmission("provider_id differs from work item")
    try:
        ensure_redacted(submission.model_dump(mode="json"), field="submission")
    except ValueError as error:
        raise InvalidSubmission(str(error)) from error
