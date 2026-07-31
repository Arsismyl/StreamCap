import asyncio
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_MODULE_PATH = Path(__file__).resolve().parent.parent / "app" / "core" / "media" / "segment_merger.py"
_spec = importlib.util.spec_from_file_location("segment_merger", _MODULE_PATH)
segment_merger = importlib.util.module_from_spec(_spec)
sys.modules["segment_merger"] = segment_merger
_spec.loader.exec_module(segment_merger)
merge_ts_segments = segment_merger.merge_ts_segments


class FakeProcess:
    def __init__(self, returncode, stderr=b""):
        self.returncode = returncode
        self._stderr = stderr

    async def communicate(self):
        return b"", self._stderr


class MergeTsSegmentsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.seg1 = os.path.join(self.dir, "base.ts")
        self.seg2 = os.path.join(self.dir, "base_001.ts")
        with open(self.seg1, "w") as f:
            f.write("AAA")
        with open(self.seg2, "w") as f:
            f.write("BBB")

    def tearDown(self):
        self._tmp.cleanup()

    def test_single_segment_is_noop(self):
        result = asyncio.run(merge_ts_segments([self.seg1]))
        self.assertEqual(result, self.seg1)

    def test_single_nonempty_segment_is_moved_to_first_path(self):
        with open(self.seg1, "w") as f:
            f.write("")  # empty first segment (first pass recorded nothing)
        result = asyncio.run(merge_ts_segments([self.seg1, self.seg2]))
        self.assertEqual(result, self.seg1)
        with open(self.seg1) as f:
            self.assertEqual(f.read(), "BBB")
        self.assertFalse(os.path.exists(self.seg2))

    async def _run_with_fake_exec(self, returncode, segment_paths):
        async def fake_exec(*args, **kwargs):
            # Last positional arg is the merged output path.
            out_path = args[-1]
            if returncode == 0:
                with open(out_path, "w") as f:
                    f.write("MERGED")
            return FakeProcess(returncode)

        with mock.patch(
            "segment_merger.asyncio.create_subprocess_exec", side_effect=fake_exec
        ):
            return await merge_ts_segments(segment_paths)

    def test_merges_two_segments_and_replaces_first(self):
        result = asyncio.run(self._run_with_fake_exec(0, [self.seg1, self.seg2]))
        self.assertEqual(result, self.seg1)
        with open(self.seg1) as f:
            self.assertEqual(f.read(), "MERGED")
        self.assertFalse(os.path.exists(self.seg2))

    def test_failed_merge_preserves_segments(self):
        result = asyncio.run(self._run_with_fake_exec(1, [self.seg1, self.seg2]))
        self.assertIsNone(result)
        self.assertTrue(os.path.exists(self.seg1))
        self.assertTrue(os.path.exists(self.seg2))


if __name__ == "__main__":
    unittest.main()
