import asyncio
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


if __name__ == "__main__":
    unittest.main()
