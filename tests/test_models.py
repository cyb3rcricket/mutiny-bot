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


@mock.patch("llm.models.subprocess.check_output")
def test_noncanonical_installed_models_are_returned(mock_check_output):
    mock_check_output.return_value = (
        "NAME ID SIZE MODIFIED\n"
        "llama3.2:latest abc 1 GB now\n"
        "gemma4:e4b def 1 GB now\n"
        "gpt-oss:cloud ghi 1 GB now\n"
        "openai/gpt-4 000 1 GB now\n"
    )
    installed = get_installed_models(force_refresh=True)
    assert installed == ["gemma4:e4b", "llama3.2:latest"]


@mock.patch("llm.models.subprocess.check_output")
def test_cache_reuses_result_until_force_refresh(mock_check_output):
    mock_check_output.return_value = "NAME ID SIZE MODIFIED\nlocal:latest abc 1 GB now\n"
    first = get_installed_models(force_refresh=True)
    second = get_installed_models(force_refresh=False)
    assert first == second == ["local:latest"]
    assert mock_check_output.call_count == 1
    get_installed_models(force_refresh=True)
    assert mock_check_output.call_count == 2


@mock.patch.dict("os.environ", {"OLLAMA_HOST": "http://example.invalid:11434"}, clear=False)
@mock.patch("llm.models.subprocess.check_output")
def test_discovery_overrides_inherited_ollama_host(mock_check_output):
    mock_check_output.return_value = "NAME ID SIZE MODIFIED\n"
    get_installed_models(force_refresh=True, api_base="http://127.0.0.1:11434")
    env = mock_check_output.call_args.kwargs["env"]
    assert env["OLLAMA_HOST"] == "127.0.0.1:11434"


def test_failed_discovery_does_not_mark_saved_model_missing_by_overwrite():
    from llm.models import DiscoveryResult, selected_model_is_available

    failed = DiscoveryResult(models=[], status="failed", checked_at="t", host="127.0.0.1:11434")
    assert selected_model_is_available("kept:latest", failed) is False
    empty = DiscoveryResult(models=[], status="empty", checked_at="t", host="127.0.0.1:11434")
    assert selected_model_is_available("kept:latest", empty) is False
    present = DiscoveryResult(models=["kept:latest"], status="ok", checked_at="t", host="127.0.0.1:11434")
    assert selected_model_is_available("kept:latest", present) is True

