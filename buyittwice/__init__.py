"""buy-it-twice: a spend gate for humans and AI agents. A purchase clears only when you could pay for it twice."""
from .core import (BAD_AMOUNT, BUY_IT_TWICE, DEFICIT, GATE_ERROR, OVER_CAP, REASON_ORDER, UNKNOWN_REQUESTER,
                   USED_UP, WOULD_GO_NEGATIVE, Config, ConfigError, Gate, Ledger, SpendDenied, check, require, void)

__version__ = "0.1.0"
__all__ = ["Config", "ConfigError", "Gate", "Ledger", "SpendDenied", "check", "require", "void", "REASON_ORDER",
           "BAD_AMOUNT", "UNKNOWN_REQUESTER", "DEFICIT", "USED_UP", "OVER_CAP", "WOULD_GO_NEGATIVE", "BUY_IT_TWICE",
           "GATE_ERROR"]
