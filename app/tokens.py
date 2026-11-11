"""Cheap token estimates for budgeting (never used as a reported measurement).

Calibration: baseline_v2.csv shows ~70 template tokens of overhead on a tiny
prompt. For long text we assume 3.5 chars/token (conservative: papers contain
maths and symbols, which tokenise poorly). The pipeline records provider-reported
`tokens_in` next to the estimate (diagnostics: est_tokens_in) so the ratio can
be checked on real runs and the constant corrected.
"""
OVERHEAD_TOKENS = 70
CHARS_PER_TOKEN = 3.5
DEFAULT_EXPECTED_OUT = 400      # baseline_v2: mean 300, p95 501 (n=5, includes reasoning)


def estimate_tokens(chars: int) -> int:
    return int(OVERHEAD_TOKENS + chars / CHARS_PER_TOKEN + 0.999)


def messages_chars(messages: list[dict]) -> int:
    return sum(len(str(m.get("content", ""))) for m in messages)
