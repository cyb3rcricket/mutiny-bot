"""Bot capability descriptions for user-facing help responses."""


def get_capabilities_response() -> str:
    """Return the canonical capability list for user-facing "what can you do" questions."""
    return (
        "I'm MutinyBot, your direct Discord assistant with real tools:\n"
        "- Morning briefing: local system snapshot and ops checklist.\n"
        "- News monitoring: RSS monitoring with deduplication and scheduled posting.\n"
        "- Eisenhower task prioritization: real urgent/important matrix classification with concrete next steps.\n"
        "- MemPalace memory: persistent memory and knowledge retrieval across conversations.\n"
        "- Local Ollama models: phi4-mini, gemma4:e4b, and qwen2.5-coder for local-first AI responses.\n"
        "- Automation scheduling: create, list, and stop recurring tool jobs."
    )
