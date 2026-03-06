"""Domain errors for governance enforcement."""


class PolicyValidationError(ValueError):
    """Raised when registry/policy/config validation fails."""


class ProposalValidationError(ValueError):
    """Raised when a proposal fails precondition validation."""


class VersionCompatibilityError(ValueError):
    """Raised when loaded artifacts are incompatible with supported versions."""
