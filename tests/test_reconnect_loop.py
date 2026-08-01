import asyncio
import collections
import os
import tempfile
import unittest
from unittest import mock

from app.core.recording.stream_manager import LiveStreamRecorder
from app.core.platforms.platform_handlers import StreamData


class FakeProcess:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.stdin = None
        self.waited = False

    def kill(self):
        pass

    def send_signal(self, sig):
        pass

    async def wait(self):
        return self.returncode

    async def communicate(self):
        return b"", (b"" if self.returncode == 0 else b"Error opening input")


class FakeServices:
    def __init__(self):
        self.settings_config = mock.MagicMock()
        self.settings_config.user_config = {}
        self.settings_config.cookies_config = {}
        self.settings_config.accounts_config = {}
        self.subprocess_start_up_info = None
        self.language_manager = mock.MagicMock()
        self.language_manager.language = {"recording_manager": {}, "stream_manager": {}}
        self.recording_manager = mock.MagicMock()
        self.recording_manager.platform_semaphores = collections.defaultdict(lambda: asyncio.Semaphore(3))
        self.config_manager = mock.MagicMock()
        self.process_manager = mock.MagicMock()
        self.recording_enabled = True

    def run_coro(self, coro):
        asyncio.ensure_future(coro)

    def snapshot_bridges(self):
        return []


def make_recorder(recording, recording_info):
    return LiveStreamRecorder(FakeServices(), recording, recording_info)


class ReconnectLoopTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.recording = mock.MagicMock()
        self.recording.rec_id = "rec-1"
        self.recording.is_live = True
        self.recording.is_recording = True
        self.recording.force_stop = False
        self.recording.manually_stopped = False
        self.recording.monitor_status = False
        self.recording.status_info = None
        self.recording.streamer_name = "test"
        self.recording.title = "test"
        self.recording.display_title = "test"
        self.recording.record_url = None

        self.info = {
            "platform": "TikTok",
            "platform_key": "tiktok",
            "live_url": "https://www.tiktok.com/@test/live",
            "output_dir": self._tmp.name,
            "segment_record": False,
            "segment_time": "1800",
            "save_format": "ts",
            "quality": "OD",
        }

    def tearDown(self):
        self._tmp.cleanup()

    async def test_reconnects_when_live_and_finalizes_when_not(self):
        recorder = make_recorder(self.recording, self.info)
        recorder.should_stop = False
        recorder.end_message_push = mock.AsyncMock()

        fetched = iter(
            [
                StreamData(platform="TikTok", anchor_name="test", is_live=True,
                           flv_url="http://cdn/stream1.flv?codec=h264&sign=a",
                           record_url="http://cdn/stream1.flv?codec=h264&sign=a"),
                StreamData(platform="TikTok", anchor_name="test", is_live=False, record_url=None),
            ]
        )
        recorder.fetch_stream = mock.AsyncMock(side_effect=fetched)

        exit_codes = iter([0, 0])
        fake_exec_count = {"n": 0}

        async def fake_exec(*args, **kwargs):
            # Write a non-empty file at the output path so the pass "recorded" content.
            out_path = args[-1]
            with open(out_path, "w") as f:
                f.write("seg")
            fake_exec_count["n"] += 1
            return FakeProcess(next(exit_codes))

        with mock.patch("asyncio.create_subprocess_exec", side_effect=fake_exec), \
             mock.patch("app.core.recording.stream_manager.merge_ts_segments", new=mock.AsyncMock(return_value=None)) as m_merge:
            await recorder.start_ffmpeg(
                "test", "https://www.tiktok.com/@test/live",
                "http://cdn/stream1.flv?sign=a", ["ffmpeg", "rec", self._tmp.name + "/out.ts"],
                "ts", None,
            )

        self.assertEqual(fake_exec_count["n"], 2)  # two ffmpeg passes
        self.assertTrue(recorder.recording.is_recording is False)
        m_merge.assert_awaited()
        # Reconnect fetches ran under the real per-platform semaphore (not a MagicMock).
        self.assertIsInstance(
            recorder.services.recording_manager.platform_semaphores, collections.defaultdict
        )
        # The reconnect segment was recorded to a distinct path.
        self.assertTrue(os.path.exists(os.path.join(self._tmp.name, "out_001.ts")))
        # No duplicate notification path: check_if_live must not be called.
        recorder.services.recording_manager.check_if_live.assert_not_called()

    async def test_merge_failure_converts_each_segment(self):
        recorder = make_recorder(self.recording, self.info)
        recorder.user_config["convert_to_mp4"] = True
        recorder.user_config["delete_original"] = True
        recorder.should_stop = False
        recorder.end_message_push = mock.AsyncMock()
        recorder.converts_mp4 = mock.AsyncMock()

        fetched = iter(
            [
                StreamData(platform="TikTok", anchor_name="test", is_live=True,
                           flv_url="http://cdn/stream1.flv?codec=h264&sign=a",
                           record_url="http://cdn/stream1.flv?codec=h264&sign=a"),
                StreamData(platform="TikTok", anchor_name="test", is_live=False, record_url=None),
            ]
        )
        recorder.fetch_stream = mock.AsyncMock(side_effect=fetched)

        exit_codes = iter([0, 0])

        async def fake_exec(*args, **kwargs):
            out_path = args[-1]
            with open(out_path, "w") as f:
                f.write("seg")
            return FakeProcess(next(exit_codes))

        with mock.patch("asyncio.create_subprocess_exec", side_effect=fake_exec), \
             mock.patch("app.core.recording.stream_manager.merge_ts_segments", new=mock.AsyncMock(return_value=None)):
            await recorder.start_ffmpeg(
                "test", "https://www.tiktok.com/@test/live",
                "http://cdn/stream1.flv?sign=a", ["ffmpeg", "rec", self._tmp.name + "/out.ts"],
                "ts", None,
            )

        await asyncio.sleep(0)  # let the run_coro-scheduled convert tasks finish
        # Merge failed → each non-empty segment must be converted individually.
        self.assertEqual(recorder.converts_mp4.await_count, 2)
        self.assertEqual(
            {os.path.normpath(call.args[0]) for call in recorder.converts_mp4.await_args_list},
            {
                os.path.normpath(os.path.join(self._tmp.name, "out.ts")),
                os.path.normpath(os.path.join(self._tmp.name, "out_001.ts")),
            },
        )

    async def test_no_reconnect_when_stream_ends_immediately(self):
        recorder = make_recorder(self.recording, self.info)
        recorder.end_message_push = mock.AsyncMock()
        recorder.fetch_stream = mock.AsyncMock(
            return_value=StreamData(platform="TikTok", anchor_name="test", is_live=False, record_url=None)
        )
        fake_exec_count = {"n": 0}

        async def fake_exec(*args, **kwargs):
            out_path = args[-1]
            with open(out_path, "w") as f:
                f.write("seg")
            fake_exec_count["n"] += 1
            return FakeProcess(0)

        with mock.patch("asyncio.create_subprocess_exec", side_effect=fake_exec), \
             mock.patch("app.core.recording.stream_manager.merge_ts_segments", new=mock.AsyncMock(return_value=None)):
            await recorder.start_ffmpeg(
                "test", "https://www.tiktok.com/@test/live",
                "http://cdn/stream1.flv?sign=a", ["ffmpeg", "rec", self._tmp.name + "/out.ts"],
                "ts", None,
            )

        self.assertEqual(fake_exec_count["n"], 1)
        self.assertFalse(recorder.recording.is_recording)

    async def test_failed_refetch_does_not_respawn_with_same_path(self):
        recorder = make_recorder(self.recording, self.info)
        recorder.should_stop = False
        recorder.end_message_push = mock.AsyncMock()
        recorder._get_retry_delay = lambda: 0

        fetched = iter(
            [
                StreamData(platform="TikTok", anchor_name="test", is_live=True,
                           flv_url="http://cdn/stream2.flv?codec=h264&sign=b",
                           record_url="http://cdn/stream2.flv?codec=h264&sign=b"),
                StreamData(platform="TikTok", anchor_name="test", is_live=False, record_url=None),
            ]
        )
        fetch_calls = {"n": 0}

        async def fake_fetch():
            fetch_calls["n"] += 1
            if fetch_calls["n"] == 1:
                raise RuntimeError("transient network failure")
            return next(fetched)

        recorder.fetch_stream = mock.AsyncMock(side_effect=fake_fetch)

        exit_codes = iter([0, 0])
        fake_exec_count = {"n": 0}
        output_paths = []

        async def fake_exec(*args, **kwargs):
            out_path = args[-1]
            output_paths.append(out_path)
            with open(out_path, "w") as f:
                f.write("seg")
            fake_exec_count["n"] += 1
            return FakeProcess(next(exit_codes))

        with mock.patch("asyncio.create_subprocess_exec", side_effect=fake_exec), \
             mock.patch("app.core.recording.stream_manager.merge_ts_segments", new=mock.AsyncMock(return_value=None)):
            await recorder.start_ffmpeg(
                "test", "https://www.tiktok.com/@test/live",
                "http://cdn/stream1.flv?sign=a", ["ffmpeg", "rec", self._tmp.name + "/out.ts"],
                "ts", None,
            )

        # A failed re-fetch must NOT respawn ffmpeg with the stale URL / same path.
        self.assertEqual(fake_exec_count["n"], 2)  # exactly two passes
        self.assertEqual(len(output_paths), 2)
        self.assertNotEqual(output_paths[0], output_paths[1])
        self.assertEqual(output_paths[1], self._tmp.name + "/out_001.ts")
        self.assertFalse(recorder.recording.is_recording)

    async def test_gives_up_after_max_reconnect_failures(self):
        recorder = make_recorder(self.recording, self.info)
        recorder.should_stop = False
        recorder.end_message_push = mock.AsyncMock()
        recorder._get_retry_delay = lambda: 0
        recorder.fetch_stream = mock.AsyncMock(side_effect=RuntimeError("stream down"))

        fake_exec_count = {"n": 0}

        async def fake_exec(*args, **kwargs):
            out_path = args[-1]
            with open(out_path, "w") as f:
                f.write("seg")
            fake_exec_count["n"] += 1
            return FakeProcess(0)

        with mock.patch("asyncio.create_subprocess_exec", side_effect=fake_exec), \
             mock.patch("app.core.recording.stream_manager.merge_ts_segments", new=mock.AsyncMock(return_value=None)):
            await recorder.start_ffmpeg(
                "test", "https://www.tiktok.com/@test/live",
                "http://cdn/stream1.flv?sign=a", ["ffmpeg", "rec", self._tmp.name + "/out.ts"],
                "ts", None,
            )

        self.assertEqual(fake_exec_count["n"], 1)  # never respawn with stale URL/path
        self.assertEqual(recorder.fetch_stream.call_count, 3)  # reconnect_max_failures attempts
        self.assertFalse(recorder.recording.is_recording)

    async def test_non_flv_platform_is_single_pass(self):
        info = dict(self.info)
        info["platform_key"] = "youtube"
        info["platform"] = "YouTube"
        recorder = make_recorder(self.recording, info)
        recorder.should_stop = False
        recorder.end_message_push = mock.AsyncMock()

        fake_exec_count = {"n": 0}

        async def fake_exec(*args, **kwargs):
            out_path = args[-1]
            with open(out_path, "w") as f:
                f.write("seg")
            fake_exec_count["n"] += 1
            return FakeProcess(0)

        with mock.patch("asyncio.create_subprocess_exec", side_effect=fake_exec), \
             mock.patch("app.core.recording.stream_manager.merge_ts_segments", new=mock.AsyncMock(return_value=None)) as m_merge:
            await recorder.start_ffmpeg(
                "test", "https://www.youtube.com/watch?v=abc",
                "http://cdn/stream1.flv?sign=a", ["ffmpeg", "rec", self._tmp.name + "/out.ts"],
                "ts", None,
            )

        self.assertEqual(fake_exec_count["n"], 1)  # single pass, reconnect is FLV-only
        m_merge.assert_not_awaited()
        self.assertFalse(recorder.recording.is_recording)


if __name__ == "__main__":
    unittest.main()
