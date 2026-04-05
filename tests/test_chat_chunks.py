"""Unit tests for chat response chunking helpers."""

import unittest

from cogs.chat import split_response_chunks


class SplitResponseChunksTests(unittest.TestCase):
    """Coverage for Discord-safe chunk splitting behavior."""

    def test_empty_text_returns_fallback_message(self) -> None:
        chunks = split_response_chunks("")
        self.assertEqual(chunks, ["I could not generate a response this time."])

    def test_hard_split_round_trip_for_dense_text(self) -> None:
        text = "x" * 123
        chunks = split_response_chunks(text, max_chunk_size=50)

        self.assertEqual([len(chunk) for chunk in chunks], [50, 50, 23])
        self.assertEqual("".join(chunks), text)

    def test_chunks_respect_max_size(self) -> None:
        text = "Line one\n" + ("word " * 100)
        chunks = split_response_chunks(text, max_chunk_size=60)

        self.assertTrue(chunks)
        self.assertTrue(all(0 < len(chunk) <= 60 for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
