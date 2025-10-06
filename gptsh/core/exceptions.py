class ToolApprovalDenied(Exception):
    """Raised when a tool call is denied and tools are required."""


class ConfigError(Exception):
    """Raised for invalid or conflicting configuration options."""
    pass
