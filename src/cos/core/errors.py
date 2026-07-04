"""Error types for the control plane."""
from __future__ import annotations


class CosError(Exception):
    """Base for all control-plane errors."""


class SpecError(CosError):
    """A malformed EnvSpec / WorkloadSpec."""


class BackendError(CosError):
    """The Docker backend failed (daemon unreachable, image not found, ...)."""


class NotFoundError(CosError):
    """A named managed container/workload was not found."""


class TimeoutError_(CosError):
    """A job exceeded its timeout."""
