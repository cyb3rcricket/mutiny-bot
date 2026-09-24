"""Offline initialization that must run before application imports.

LiteLLM 1.82.6 fetches a remote model-cost map during import unless
LITELLM_LOCAL_MODEL_COST_MAP is true. Set that, and the telemetry switches
this version actually reads, before any test module imports the package.
"""

from __future__ import annotations

import os

os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["ANONYMIZED_TELEMETRY"] = "False"
os.environ["DO_NOT_TRACK"] = "1"
os.environ.setdefault("OLLAMA_HOST", "127.0.0.1:11434")


def pytest_configure() -> None:
    """Keep callback and cost-map traffic off if LiteLLM is already imported."""
    try:
        import litellm
    except Exception:
        return
    if hasattr(litellm, "telemetry"):
        litellm.telemetry = False
    callbacks = getattr(litellm, "success_callback", None)
    if isinstance(callbacks, list):
        callbacks.clear()
    failure_callbacks = getattr(litellm, "failure_callback", None)
    if isinstance(failure_callbacks, list):
        failure_callbacks.clear()
