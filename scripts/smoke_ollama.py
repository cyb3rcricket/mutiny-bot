"""Opt-in smoke check against a local Ollama server.

Performs a real completion request. Runs only when executed directly so test
collection never imports LiteLLM or contacts Ollama.
"""

from __future__ import annotations


def main() -> int:
    import asyncio

    import litellm

    async def _probe() -> int:
        try:
            response = await litellm.acompletion(
                model="ollama/gemma4:e4b",
                messages=[{"role": "user", "content": "Hello! How are you?"}],
                api_base="http://127.0.0.1:11434",
                timeout=10.0,
            )
            print(response)
            return 0
        except Exception as exc:
            print("ERROR:", exc)
            return 1

    return asyncio.run(_probe())


if __name__ == "__main__":
    raise SystemExit(main())
