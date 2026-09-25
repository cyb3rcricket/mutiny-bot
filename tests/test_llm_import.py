"""Offline import of the inference handler. This must not call Ollama."""


def test_llm_handler_imports() -> None:
    import llm.llm_handler

    assert llm.llm_handler.LLMHandler is not None
    assert llm.llm_handler.LLMError is not None
