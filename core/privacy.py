"""Process-wide privacy bootstrap.

Must run before LiteLLM or MemPalace are imported. LiteLLM 1.82.6 reads
LITELLM_LOCAL_MODEL_COST_MAP during import and otherwise GETs a remote cost map.
"""

from __future__ import annotations

import os

_BOOTSTRAPPED = False


def bootstrap() -> None:
    """Apply offline settings. Safe to call more than once."""
    global _BOOTSTRAPPED
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["ANONYMIZED_TELEMETRY"] = "False"
    os.environ["DO_NOT_TRACK"] = "1"
    os.environ.setdefault("OLLAMA_HOST", "127.0.0.1:11434")
    if _BOOTSTRAPPED:
        return
    _BOOTSTRAPPED = True

    try:
        import litellm
    except Exception:
        return

    # This pin defines the attribute and never reads it. Set it anyway, then
    # prove silence with an egress test rather than trusting the flag.
    if hasattr(litellm, "telemetry"):
        litellm.telemetry = False
    for name in ("success_callback", "failure_callback", "_async_success_callback", "_async_failure_callback"):
        callbacks = getattr(litellm, name, None)
        if isinstance(callbacks, list):
            callbacks.clear()
    litellm.turn_off_message_logging = True
