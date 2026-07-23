"""Typed failures for multi-fidelity evaluation."""


class EvalError(Exception):
    """Base class for evaluation protocol failures."""


class InvalidSubmission(EvalError):
    """A submission violates its work-item contract."""


class DuplicateSubmission(EvalError):
    """A completed work item received a different second submission."""


class PartialArtifact(EvalError):
    """An artifact is missing or does not match its declared content hash."""


class UnsupportedCapability(EvalError):
    """A provider cannot satisfy a requested evidence tier or capability."""


class PairNotComparable(EvalError):
    """Paired records differ in a field other than the permitted variant snapshot."""


class WorkTimeout(EvalError):
    """The delegated work exceeded its registered deadline."""
