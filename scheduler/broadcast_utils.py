"""Utilities for splitting scheduler broadcast payloads into Discord-safe chunks."""

DISCORD_SAFE_BROADCAST_CHARS = 1950


def split_broadcast_chunks(content: str, max_chunk_size: int = DISCORD_SAFE_BROADCAST_CHARS) -> list[str]:
    """Split broadcast text into Discord-safe chunks, favoring newline boundaries."""
    text = str(content or "").strip()
    if not text:
        return []

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_chunk_size:
            chunks.append(remaining)
            break

        window = remaining[:max_chunk_size]
        split_at = window.rfind("\n")
        if split_at == -1:
            split_at = window.rfind(" ")
        if split_at < max_chunk_size // 2:
            split_at = max_chunk_size

        chunk = remaining[:split_at].rstrip()
        if not chunk:
            chunk = remaining[:max_chunk_size]
            split_at = len(chunk)

        chunks.append(chunk)
        remaining = remaining[split_at:].lstrip("\n")

    return chunks
