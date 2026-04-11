import asyncio
import litellm
import sys

async def test():
    try:
        response = await litellm.acompletion(
            model="ollama/gemma4:e4b",
            messages=[{"role": "user", "content": "Hello! How are you?"}],
            api_base="http://127.0.0.1:11434",
            timeout=10.0,
        )
        print(response)
    except Exception as e:
        print("ERROR:", e)

asyncio.run(test())
