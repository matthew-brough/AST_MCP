"""SPEC §T.3 — stat-keyed cache, byte-exact slices, non-raising failures."""

import os
import tempfile
import unittest
from pathlib import Path

from ast_mcp import parser


class ParserTestBase(unittest.TestCase):
    def setUp(self):
        parser.cache_clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, name: str, data: bytes) -> Path:
        p = self.root / name
        p.write_bytes(data)
        return p


class TestCache(ParserTestBase):
    def test_repeat_parse_is_a_cache_hit(self):
        p = self.write("a.py", b"def f():\n    pass\n")
        first, _ = parser.parse_file(p)
        second, _ = parser.parse_file(p)
        self.assertIsNotNone(first)
        self.assertIs(first, second, "same stat key must reuse the cached tree")

    def test_edit_invalidates_cache(self):
        """SPEC §V.3 — a changed file is reparsed before it is answered from."""
        p = self.write("a.py", b"def f():\n    pass\n")
        first, _ = parser.parse_file(p)
        p.write_bytes(b"def g():\n    pass\n")
        # The edit keeps the size, so mtime is the only signal — and Windows
        # ticks its clock about every 16ms, which is long enough for both
        # writes to land on one timestamp. Stamp it so this measures
        # invalidation rather than the host's timer resolution.
        moved = first.mtime_ns + 10**9
        os.utime(p, ns=(moved, moved))
        second, _ = parser.parse_file(p)
        self.assertIsNot(first, second)
        self.assertIn(b"def g", second.source)

    def test_cache_is_bounded(self):
        for i in range(parser._CACHE_SIZE + 10):
            parser.parse_file(self.write(f"m{i}.py", b"x = 1\n"))
        self.assertLessEqual(parser.cache_size(), parser._CACHE_SIZE)


class TestByteTruth(ParserTestBase):
    def test_slice_is_byte_exact_with_crlf_and_astral_chars(self):
        """SPEC §V.2 — slices index bytes, so CRLF and non-BMP survive."""
        src = 'def f():\r\n    return "🎯 tail"\r\n'.encode()
        p = self.write("crlf.py", src)
        parsed, _ = parser.parse_file(p)
        node = parsed.tree.root_node.children[0]
        self.assertEqual(
            parsed.slice(node.start_byte, node.end_byte),
            src[node.start_byte : node.end_byte].decode(),
        )
        self.assertIn("🎯", parsed.slice(0, len(src)))

    def test_line_count(self):
        parsed, _ = parser.parse_file(self.write("n.py", b"a = 1\nb = 2\n"))
        self.assertEqual(parsed.line_count, 2)


class TestDegradation(ParserTestBase):
    def test_unsupported_extension(self):
        parsed, errors = parser.parse_file(self.write("notes.xyz", b"hi"))
        self.assertIsNone(parsed)
        self.assertEqual([e.code for e in errors], ["unsupported_lang"])

    def test_missing_file(self):
        parsed, errors = parser.parse_file(self.root / "ghost.py")
        self.assertIsNone(parsed)
        self.assertEqual([e.code for e in errors], ["unreadable"])

    def test_oversized_file(self):
        big = b"x = 1\n" * (parser.MAX_BYTES // 6 + 10)
        parsed, errors = parser.parse_file(self.write("big.py", big))
        self.assertIsNone(parsed)
        self.assertEqual([e.code for e in errors], ["too_large"])

    def test_syntax_error_is_reported_not_raised(self):
        """SPEC §V.5 — a broken file still parses into a usable tree."""
        parsed, errors = parser.parse_file(self.write("bad.py", b"def (:\n"))
        self.assertIsNotNone(parsed)
        self.assertEqual([e.code for e in errors], ["syntax_error"])


if __name__ == "__main__":
    unittest.main()
