"""Hard/soft validation rules producing a structured report (pure, no I/O)."""

from doc_agent.validation.rules import (
    RuleResult,
    ValidationReport,
    money_close,
    validate,
)

__all__ = ["RuleResult", "ValidationReport", "money_close", "validate"]
