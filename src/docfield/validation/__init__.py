"""Hard/soft validation rules producing a structured report (pure, no I/O)."""

from docfield.validation.rules import (
    RuleResult,
    ValidationReport,
    money_close,
    validate,
)

__all__ = ["RuleResult", "ValidationReport", "money_close", "validate"]
