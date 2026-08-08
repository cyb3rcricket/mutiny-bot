import subprocess
from unittest import mock
import pytest

from llm.models import get_installed_models, CANONICAL_MODELS

@mock.patch("llm.models.subprocess.check_output")
def test_get_installed_models_ollama_down(mock_check_output):
    """Ensure that if ollama is down (FileNotFoundError), it returns an empty list, not spoofed models."""
    mock_check_output.side_effect = FileNotFoundError()
    # Ensure cache is bypassed or we force refresh
    installed = get_installed_models(force_refresh=True)
    assert installed == [], f"Expected empty list when Ollama is down, got {installed}"

@mock.patch("llm.models.subprocess.check_output")
def test_get_installed_models_ollama_timeout(mock_check_output):
    """Ensure that if ollama times out, it returns an empty list."""
    mock_check_output.side_effect = subprocess.TimeoutExpired(cmd="ollama list", timeout=3)
    installed = get_installed_models(force_refresh=True)
    assert installed == [], f"Expected empty list on timeout, got {installed}"

@mock.patch("llm.models.subprocess.check_output")
def test_get_installed_models_empty_list(mock_check_output):
    """Ensure that if ollama returns empty (no models), it returns an empty list."""
    mock_check_output.return_value = "NAME ID SIZE MODIFIED\n"
    installed = get_installed_models(force_refresh=True)
    assert installed == [], f"Expected empty list when no models installed, got {installed}"

