"""Unit tests for scheduler broadcast chunk splitting."""

import unittest

from scheduler.broadcast_utils import split_broadcast_chunks


class SchedulerBroadcastChunkTests(unittest.TestCase):
    """Ensure broadcast payloads are split into Discord-safe chunks."""

    def test_empty_content_returns_no_chunks(self) -> None:
        self.assertEqual(split_broadcast_chunks(""), [])

    def test_hard_split_round_trip(self) -> None:
        text = "x" * 4300
        chunks = split_broadcast_chunks(text, max_chunk_size=1950)

        self.assertTrue(chunks)
        self.assertTrue(all(0 < len(chunk) <= 1950 for chunk in chunks))
        self.assertEqual("".join(chunks), text)

    def test_prefers_newline_boundaries_when_possible(self) -> None:
        text = "line1\n" + ("word " * 700)
        chunks = split_broadcast_chunks(text, max_chunk_size=500)

        self.assertTrue(chunks)
        self.assertTrue(all(len(chunk) <= 500 for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
