"""M1E: opt-in paper-only, alerting second-book edge model for the 1m scan.

The package is intentionally independent of ``nse_scanner`` so it can be
dropped into the external 1m runtime without changing the original scanner.
"""
from .model import (
    SCHEMA,
    Config,
    SecondBookModel,
    evaluate_if_enabled,
    finalize_if_enabled,
    format_bar_alert,
    format_eod_alert,
    from_board_payload,
    maybe_finalize,
    maybe_observe,
)

__all__ = [
    "SCHEMA", "Config", "SecondBookModel", "evaluate_if_enabled",
    "finalize_if_enabled", "format_bar_alert", "format_eod_alert",
    "from_board_payload", "maybe_observe", "maybe_finalize",
]
