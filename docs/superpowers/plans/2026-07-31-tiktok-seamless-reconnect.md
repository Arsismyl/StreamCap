# TikTok Seamless Reconnect Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When TikTok/Douyin (FLV) live streams drop mid-broadcast, reconnect within seconds and merge segments into one continuous file, without duplicate "live started" notifications.

**Architecture:** Extract a standalone `segment_merger` module for the lossless TS merge (stdlib-only, locally testable). Refactor `LiveStreamRecorder` in `stream_manager.py` so the FFmpeg run is a loop: run one FFmpeg process → on exit, if FLV platform & still live, re-fetch stream data and restart with a fresh signed URL into the next `_001.ts` segment → when the live truly ends, merge segments. Behavior is identical for all non-FLV platforms.

**Tech Stack:** Python 3.12, asyncio, FFmpeg subprocess, FFmpeg concat demuxer. No new runtime dependencies.

## Global Constraints

- Reconnect behavior applies **only** to `self.is_flv_preferred_platform` (tiktok, douyin) **and** `not self.segment_record`. Every other platform and every `segment_record=True` recording keeps today's exact behavior.
- Merge is performed **only** when `self.save_format == "ts"` (TS is concat-safe). For other save formats, reconnect still happens but segments stay separate.
- **Never delete a recorded segment before a verified merge** (`os.replace` only after the merged file exists and is non-empty).
- Give up reconnecting after **3 consecutive** failed re-fetches (no infinite loop).
- Reconnect calls `fetch_stream()` directly — **never** `check_if_live()` — so the "live started" notification is not re-sent and `recording.is_live` stays `True`.
- No new settings, no UI changes, no changes to `record_manager.py`.
- Style: line-length 120, double quotes, existing ruff rules. New tests use stdlib `unittest`.
- Local Python 3.12 has no `flet`/`streamget`; the merge module and its tests must run without them.

---

### Task 1: Standalone TS segment merger module + unit tests

**Files:**
- Create: `app/core/media/segment_merger.py`
- Create: `tests/test_segment_merger.py`
- Modify: `.ruff.toml` (ignore pytest-style rules in `tests/`)

**Interfaces:**
- Produces: `async def merge_ts_segments(segment_paths: list[str], startupinfo=None) -> str | None` — merges non-empty TS segments in order into the first segment's path; returns the merged path, or `None` on failure (original segments preserved).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_segment_merger.py`:

```python
import asyncio
import os
import tempfile
import unittest
from unittest import mock

from app.core.media.segment_merger import merge_ts_segments


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

    async def _run_with_fake_exec(self, returncode, segment_paths):
        async def fake_exec(*args, **kwargs):
            # Last positional arg is the merged output path.
            out_path = args[-1]
            if returncode == 0:
                with open(out_path, "w") as f:
                    f.write("MERGED")
            return FakeProcess(returncode)

        with mock.patch(
            "app.core.media.segment_merger.asyncio.create_subprocess_exec", side_effect=fake_exec
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_segment_merger -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.core.media.segment_merger'`

- [ ] **Step 3: Write the module**

Create `app/core/media/segment_merger.py`:

```python
import asyncio
import os


def _escape_concat_path(path: str) -> str:
    return path.replace("'", "'\\''")


async def merge_ts_segments(segment_paths: list[str], startupinfo=None) -> str | None:
    """Losslessly merge non-empty TS segments (in order) into the first segment's path.

    Returns the merged path on success, or None on failure with the original
    segment files preserved.
    """
    valid = [p for p in segment_paths if os.path.exists(p) and os.path.getsize(p) > 0]
    if len(valid) <= 1:
        return valid[0] if valid else None

    first = segment_paths[0]
    base, ext = os.path.splitext(first)
    merged_temp = f"{base}.merged{ext}"
    dir_name = os.path.dirname(first) or "."
    list_path = os.path.join(dir_name, f".concat_{os.path.basename(first)}.txt")

    try:
        with open(list_path, "w", encoding="utf-8") as f:
            for p in valid:
                f.write(f"file '{_escape_concat_path(p)}'\n")

        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", list_path,
            "-c", "copy",
            merged_temp,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            startupinfo=startupinfo,
        )
        _, stderr = await proc.communicate()
        if proc.returncode == 0 and os.path.exists(merged_temp) and os.path.getsize(merged_temp) > 0:
            os.replace(merged_temp, first)
            for p in valid:
                if p != first:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            return first
        return None
    except OSError:
        return None
    finally:
        for cleanup in (list_path, merged_temp):
            if os.path.exists(cleanup):
                try:
                    os.remove(cleanup)
                except OSError:
                    pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_segment_merger -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Add ruff ignore for tests**

Append to `tests/*` in `.ruff.toml`:

```toml
"tests/*" = [
    "PT", # pytest-style rules (we use unittest)
]
```

- [ ] **Step 6: Commit**

```bash
git add app/core/media/segment_merger.py tests/test_segment_merger.py .ruff.toml
git commit -m "feat: add standalone TS segment merger with unit tests"
```

---

### Task 2: Local test environment (venv + app deps)

**Files:**
- Create: `.venv-test/` (gitignored; for local import/testing only)

**Interfaces:**
- Consumes: nothing.
- Produces: a Python environment where `import app.core.recording.stream_manager` succeeds, enabling the mock integration test in Task 4.

- [ ] **Step 1: Add `.venv-test/` to `.gitignore`**

Append to `.gitignore`:

```
.venv-test/
```

- [ ] **Step 2: Create venv and install deps**

```bash
python -m venv .venv-test
.venv-test/Scripts/python -m pip install --upgrade pip
.venv-test/Scripts/python -m pip install -r requirements.txt
.venv-test/Scripts/python -m pip install "git+https://github.com/Arsismyl/streamget.git@main"
```

Expected: all packages install. If a package fails to build on Windows (e.g. a native dep), note the error and continue; the authoritative verification for the reconnect loop is then the server test in Task 5.

- [ ] **Step 3: Verify the module imports**

Run: `.venv-test/Scripts/python -c "import app.core.recording.stream_manager"`
Expected: no exception. If it fails, record the missing module and note that Task 4's integration test is skipped; `py_compile` + server verification still apply.

- [ ] **Step 4: Commit**

```bash
git add .gitignore
git commit -m "chore: gitignore local test venv"
```

---

### Task 3: Refactor `stream_manager.py` — extract helpers (behavior-preserving)

**Files:**
- Modify: `app/core/recording/stream_manager.py`

**Interfaces:**
- Produces (used by Task 4):
  - `self.segment_paths: list[str]`
  - `self.reconnect_max_failures = 3`
  - `def _build_ffmpeg_command(self, record_url: str, full_path: str) -> list` — builds the FFmpeg command for a given URL/output path (extracted from `start_recording`).
  - `def _get_retry_delay(self) -> int` — clamps `recording_retry_interval` to 1..60, default 5.
  - `async def _run_ffmpeg_pass(self, record_name, live_url, record_url, ffmpeg_command, keep_active: bool) -> tuple[int, bytes]` — spawns one FFmpeg process, monitors it, returns `(return_code, stderr)`.
  - `def _segment_save_path(self, base_save_path: str, index: int) -> str`
  - `def _has_recorded_content(self) -> bool`

This task makes **no behavior change**; it only moves code so the reconnect loop in Task 4 has clean building blocks.

- [ ] **Step 1: Add state attributes to `__init__`**

After `self.recording_start_time = 0` (near line 51 of `stream_manager.py`):

```python
self.segment_paths: list[str] = []
self.reconnect_max_failures = 3
```

- [ ] **Step 2: Extract `_build_ffmpeg_command` and update `start_recording`**

In `start_recording`, replace the inline builder block (currently lines ~310-326) with:

```python
ffmpeg_command = self._build_ffmpeg_command(record_url, save_path)
```

Add this method (content taken verbatim from the replaced block):

```python
def _build_ffmpeg_command(self, record_url: str, full_path: str) -> list:
    header_params = self.get_headers_params(record_url, self.platform_key)
    cookie_str = None
    if self.platform_key in ("youtube", "twitcasting") and self.cookies:
        cookie_str = "\n".join(c.strip() for c in self.cookies.split(";"))
    ffmpeg_builder = ffmpeg_builders.create_builder(
        self.save_format,
        record_url=record_url,
        proxy=self.proxy,
        segment_record=self.segment_record,
        segment_time=self.segment_time,
        full_path=full_path,
        headers=header_params,
        cookies=cookie_str,
    )
    return ffmpeg_builder.build_command()
```

- [ ] **Step 3: Add `_get_retry_delay` and replace the inline retry-delay code**

Add:

```python
def _get_retry_delay(self) -> int:
    try:
        retry_delay = int(self.user_config.get("recording_retry_interval", 5))
    except (ValueError, TypeError):
        retry_delay = 5
    return max(1, min(retry_delay, 60))
```

In the existing 5XX auto-retry block in `start_ffmpeg` (currently lines ~441-444), replace:

```python
retry_delay = int(self.user_config.get("recording_retry_interval", 5))
```
…and the subsequent try/except/clamp with:

```python
await asyncio.sleep(self._get_retry_delay())
```

(remove the now-unused `retry_delay = ...` lines and the log line that printed `{retry_delay}s` — the log line becomes `Auto-retry recording after retry delay: {live_url}`.)

- [ ] **Step 4: Add `_segment_save_path` and `_has_recorded_content`**

```python
def _segment_save_path(self, base_save_path: str, index: int) -> str:
    base, ext = os.path.splitext(base_save_path)
    return f"{base}_{index:03d}{ext}"

def _has_recorded_content(self) -> bool:
    for path in self.segment_paths:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return True
    return False
```

- [ ] **Step 5: Extract `_run_ffmpeg_pass`**

Move the spawn + monitor loop + graceful-stop + `communicate()` body out of `start_ffmpeg` (currently lines ~372-430) into:

```python
async def _run_ffmpeg_pass(
    self, record_name: str, live_url: str, record_url: str, ffmpeg_command: list, keep_active: bool
) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(
        *ffmpeg_command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        startupinfo=self.subprocess_start_info,
    )

    self.services.process_manager.add_process(process)
    self.recording.status_info = RecordingStatus.RECORDING
    self.recording.record_url = record_url
    logger.info(f"Recording in Progress: {live_url}")
    logger.log("STREAM", f"Recording Stream URL: {record_url}")
    if self.recording_start_time == 0:
        self.recording_start_time = time.time()

    while True:
        if self.should_stop or self.recording.force_stop or not self.services.recording_enabled:
            logger.info(f"Preparing to End Recording: {live_url}")
            if not keep_active:
                await self.remove_active_recorder()
                self.recording.is_recording = False
            try:
                if os.name == "nt":
                    if process.stdin:
                        process.stdin.write(b"q")
                        await process.stdin.drain()
                        await asyncio.sleep(5)
                else:
                    import signal

                    process.send_signal(signal.SIGINT)
                    await asyncio.sleep(5)

                if process.stdin:
                    process.stdin.close()

                await asyncio.wait_for(process.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                logger.warning(f"FFmpeg process did not exit gracefully, forcing termination: {live_url}")
                process.kill()
                await process.wait()

            self.recording.force_stop = False
            break

        if process.returncode is not None:
            logger.info(f"Exit loop recording (normal 0 | abnormal 1): code={process.returncode}, {live_url}")
            if not keep_active:
                await self.remove_active_recorder()
                self.recording.is_recording = False
            break

        await asyncio.sleep(1)

    return_code = process.returncode
    stdout, stderr = await process.communicate()
    return return_code, stderr
```

Note: `stdout` is intentionally unused (kept for symmetry with the original `process.communicate()`). When `keep_active=True`, the recorder stays registered in `active_recorders` and `recording.is_recording` stays `True` across passes — this blocks concurrent `check_if_live` during reconnects.

- [ ] **Step 6: Rewire `start_ffmpeg` to call `_run_ffmpeg_pass` (single pass only, still no reconnect)**

Replace the whole body of `start_ffmpeg` with:

```python
async def start_ffmpeg(
    self,
    record_name: str,
    live_url: str,
    record_url: str,
    ffmpeg_command: list,
    save_type: str,
    script_command: str | None = None,
) -> bool:
    logger.info(f"Starting ffmpeg recording - recorder id: {id(self)}, rec_id: {self.recording.rec_id}")
    self.should_stop = False
    self.segment_paths = []

    try:
        save_file_path = ffmpeg_command[-1]
        self.segment_paths.append(save_file_path)
        keep_active = False
        reconnect_enabled = False
        return_code = None
        stderr = None

        while True:
            return_code, stderr = await self._run_ffmpeg_pass(
                record_name, live_url, record_url, ffmpeg_command, keep_active=keep_active
            )
            break  # no reconnect in this task; loop structure is for Task 4

        await self.remove_active_recorder()
        self.recording.is_recording = False

        safe_return_code = [0, 255]

        if return_code not in safe_return_code and stderr:
            if not self.recording.is_recording:
                logger.error(f"FFmpeg Stderr Output: {str(stderr.decode()).splitlines()[0]}")
                self._handle_recording_error(record_name, self._["record_stream_error"])

                # Auto-retry once after transient CDN error (e.g. TikTok 5xx)
                if not self.should_stop and not getattr(self, "_retried", False):
                    self._retried = True
                    await asyncio.sleep(self._get_retry_delay())
                    logger.info(f"Auto-retry recording after retry delay: {live_url}")
                    self.services.run_coro(self.services.recording_manager.check_if_live(self.recording))

        if return_code in safe_return_code:
            if not self.recording.is_recording:
                await self._handle_recording_finished(record_name)

            if not self.services.recording_enabled:
                self.recording.status_info = RecordingStatus.NOT_RECORDING_SPACE
                self.services.run_coro(self.stop_recording_notify())

            if not self.recording.manually_stopped:
                await self.recheck_live_status()

            if self.user_config.get("convert_to_mp4") and self.save_format == "ts":
                if self.segment_record:
                    file_paths = utils.get_file_paths(os.path.dirname(self.segment_paths[0]))
                    prefix = os.path.basename(self.segment_paths[0]).rsplit("_", maxsplit=1)[0]
                    for path in file_paths:
                        if prefix in path:
                            try:
                                self.services.run_coro(self.converts_mp4(path, self.user_config["delete_original"]))
                            except Exception as e:
                                logger.error(f"Failed to convert video: {e}")
                                await self.converts_mp4(path, self.user_config["delete_original"])
                else:
                    try:
                        self.services.run_coro(
                            self.converts_mp4(self.segment_paths[0], self.user_config["delete_original"])
                        )
                    except Exception as e:
                        logger.error(f"Failed to convert video: {e}")
                        await self.converts_mp4(self.segment_paths[0], self.user_config["delete_original"])

            if self.user_config.get("execute_custom_script") and script_command:
                logger.info("Prepare a direct script in the background")
                try:
                    self.services.run_coro(
                        self.custom_script_execute(
                            script_command,
                            record_name,
                            self.segment_paths[0],
                            save_type,
                            self.segment_record,
                            self.user_config.get("convert_to_mp4"),
                        )
                    )
                    logger.success("Successfully added script execution")
                except Exception as e:
                    logger.error(f"Failed to execute custom script: {e}")
                    await self.custom_script_execute(
                        script_command,
                        record_name,
                        self.segment_paths[0],
                        save_type,
                        self.segment_record,
                        self.user_config.get("convert_to_mp4"),
                    )

    except Exception as e:
        logger.error(f"An error occurred during the subprocess execution: {e}")
        self._handle_recording_error(record_name, self._["no_ffmpeg_tip"], duration=4000)
        return False
    finally:
        self.recording.record_url = None

    return True
```

The only intentional semantic changes vs. the original: references to `save_file_path` in the convert/custom-script blocks now use `self.segment_paths[0]` (which is `save_file_path` in this task). Everything else is byte-identical behavior.

- [ ] **Step 7: Syntax check**

Run: `python -m py_compile app/core/recording/stream_manager.py`
Expected: exit 0, no output.

- [ ] **Step 8: If Task 2 succeeded, run a baseline import**

Run: `.venv-test/Scripts/python -c "import app.core.recording.stream_manager"`
Expected: no exception.

- [ ] **Step 9: Commit**

```bash
git add app/core/recording/stream_manager.py
git commit -m "refactor: extract ffmpeg run helper and command builder in stream_manager"
```

---

### Task 4: Reconnect loop + segment merge in `start_ffmpeg`

**Files:**
- Modify: `app/core/recording/stream_manager.py`
- Create: `tests/test_reconnect_loop.py` (runs only if Task 2 env is available)

**Interfaces:**
- Consumes: `_run_ffmpeg_pass`, `_build_ffmpeg_command`, `_segment_save_path`, `_has_recorded_content`, `_get_retry_delay`, `segment_paths`, `reconnect_max_failures`, `merge_ts_segments`.
- Produces: reconnecting `start_ffmpeg` and the finalize/merge wiring.

- [ ] **Step 1: Write the failing integration test**

Create `tests/test_reconnect_loop.py` (unittest; run only in the Task 2 venv):

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run (in venv): `.venv-test/Scripts/python -m unittest tests.test_reconnect_loop -v`
Expected: FAIL — the current `start_ffmpeg` runs only one pass, so `test_reconnects_when_live_and_finalizes_when_not` sees `fake_exec_count["n"] == 1` (assert fails), and `merge_ts_segments` is never awaited.

- [ ] **Step 3: Implement the reconnect loop**

Replace the body of `start_ffmpeg` (from Task 3) with:

```python
async def start_ffmpeg(
    self,
    record_name: str,
    live_url: str,
    record_url: str,
    ffmpeg_command: list,
    save_type: str,
    script_command: str | None = None,
) -> bool:
    logger.info(f"Starting ffmpeg recording - recorder id: {id(self)}, rec_id: {self.recording.rec_id}")
    self.should_stop = False
    self.segment_paths = []

    try:
        save_file_path = ffmpeg_command[-1]
        self.segment_paths.append(save_file_path)
        segment_index = 0
        reconnect_failures = 0
        return_code = None
        stderr = None

        reconnect_enabled = self.is_flv_preferred_platform and not self.segment_record
        keep_active = reconnect_enabled

        while True:
            return_code, stderr = await self._run_ffmpeg_pass(
                record_name, live_url, record_url, ffmpeg_command, keep_active=keep_active
            )

            if not reconnect_enabled:
                break

            if self.should_stop or self.recording.force_stop or not self.services.recording_enabled:
                break

            try:
                stream_info = await self.fetch_stream()
            except Exception as e:
                logger.warning(f"Reconnect stream fetch failed: {e}")
                stream_info = None

            if stream_info is None:
                reconnect_failures += 1
                if reconnect_failures >= self.reconnect_max_failures:
                    logger.warning(f"Giving up reconnect after {reconnect_failures} failed attempts: {live_url}")
                    break
                await asyncio.sleep(self._get_retry_delay())
                continue

            if not stream_info.is_live or not stream_info.record_url:
                logger.info(f"Stream ended, finalizing recording: {live_url}")
                break

            segment_index += 1
            save_file_path = self._segment_save_path(self.segment_paths[0], segment_index)
            self.segment_paths.append(save_file_path)
            record_url = self._get_record_url(stream_info)
            ffmpeg_command = self._build_ffmpeg_command(record_url, save_file_path)
            reconnect_failures = 0
            logger.info(f"Reconnecting segment {segment_index}: {save_file_path}")

        await self.remove_active_recorder()
        self.recording.is_recording = False

        merged = None
        if reconnect_enabled and self.save_format == "ts" and len(self.segment_paths) > 1:
            merged = await merge_ts_segments(self.segment_paths, startupinfo=self.subprocess_start_info)
            if merged:
                logger.success(f"Merged {len(self.segment_paths)} segments into {merged}")
            else:
                logger.warning("Segment merge failed, keeping segment files")

        safe_return_code = [0, 255]
        has_content = self._has_recorded_content()

        if return_code not in safe_return_code and stderr:
            if not self.recording.is_recording:
                if reconnect_enabled and has_content:
                    logger.warning(
                        f"FFmpeg exited with code {return_code} but {len(self.segment_paths)} segment(s) recorded; finalizing"
                    )
                else:
                    logger.error(f"FFmpeg Stderr Output: {str(stderr.decode()).splitlines()[0]}")
                    self._handle_recording_error(record_name, self._["record_stream_error"])

                    # Auto-retry once after transient CDN error — non-FLV platforms only.
                    if not self.should_stop and not reconnect_enabled and not getattr(self, "_retried", False):
                        self._retried = True
                        await asyncio.sleep(self._get_retry_delay())
                        logger.info(f"Auto-retry recording after retry delay: {live_url}")
                        self.services.run_coro(self.services.recording_manager.check_if_live(self.recording))

        if return_code in safe_return_code or (reconnect_enabled and has_content):
            if not self.recording.is_recording:
                await self._handle_recording_finished(record_name)

            if not self.services.recording_enabled:
                self.recording.status_info = RecordingStatus.NOT_RECORDING_SPACE
                self.services.run_coro(self.stop_recording_notify())

            if not self.recording.manually_stopped:
                await self.recheck_live_status()

            if self.user_config.get("convert_to_mp4") and self.save_format == "ts":
                if self.segment_record:
                    file_paths = utils.get_file_paths(os.path.dirname(self.segment_paths[0]))
                    prefix = os.path.basename(self.segment_paths[0]).rsplit("_", maxsplit=1)[0]
                    for path in file_paths:
                        if prefix in path:
                            try:
                                self.services.run_coro(self.converts_mp4(path, self.user_config["delete_original"]))
                            except Exception as e:
                                logger.error(f"Failed to convert video: {e}")
                                await self.converts_mp4(path, self.user_config["delete_original"])
                else:
                    try:
                        self.services.run_coro(
                            self.converts_mp4(self.segment_paths[0], self.user_config["delete_original"])
                        )
                    except Exception as e:
                        logger.error(f"Failed to convert video: {e}")
                        await self.converts_mp4(self.segment_paths[0], self.user_config["delete_original"])

            if self.user_config.get("execute_custom_script") and script_command:
                logger.info("Prepare a direct script in the background")
                try:
                    self.services.run_coro(
                        self.custom_script_execute(
                            script_command,
                            record_name,
                            self.segment_paths[0],
                            save_type,
                            self.segment_record,
                            self.user_config.get("convert_to_mp4"),
                        )
                    )
                    logger.success("Successfully added script execution")
                except Exception as e:
                    logger.error(f"Failed to execute custom script: {e}")
                    await self.custom_script_execute(
                        script_command,
                        record_name,
                        self.segment_paths[0],
                        save_type,
                        self.segment_record,
                        self.user_config.get("convert_to_mp4"),
                    )

    except Exception as e:
        logger.error(f"An error occurred during the subprocess execution: {e}")
        self._handle_recording_error(record_name, self._["no_ffmpeg_tip"], duration=4000)
        return False
    finally:
        self.recording.record_url = None

    return True
```

Add the import at the top of `stream_manager.py`:

```python
from ..media.segment_merger import merge_ts_segments
```

- [ ] **Step 4: Syntax check**

Run: `python -m py_compile app/core/recording/stream_manager.py`
Expected: exit 0.

- [ ] **Step 5: Run the integration tests**

Run (in venv): `.venv-test/Scripts/python -m unittest tests.test_reconnect_loop tests.test_segment_merger -v`
Expected: PASS.

If the Task 2 venv is unavailable: state that explicitly; these tests are then deferred to the server in Task 5.

- [ ] **Step 6: Commit**

```bash
git add app/core/recording/stream_manager.py tests/test_reconnect_loop.py
git commit -m "feat: reconnect TikTok/Douyin recording on CDN drop and merge segments"
```

---

### Task 5: Final review + push + server verification

- [ ] **Step 1: Re-read the full diff and self-review**

Run: `git diff 95ffcbc -- app/core/recording/stream_manager.py app/core/media/segment_merger.py`
Check against the design doc:
1. Reconnect only for FLV platforms and `not segment_record`.
2. Reconnect uses `fetch_stream()` directly (no `check_if_live`) → no duplicate notifications.
3. `recording.is_live` stays `True` across reconnects; only finalize sets it `False`.
4. Merge only for `save_format == "ts"` and `len(segment_paths) > 1`; segments preserved on failure.
5. Give up after 3 consecutive failed re-fetches.
6. Convert/custom-script operate on `self.segment_paths[0]` (the merged file).

- [ ] **Step 2: Run full local test suite**

Run: `python -m unittest tests.test_segment_merger -v`
Expected: PASS.

- [ ] **Step 3: Push**

```bash
git push origin main
```

- [ ] **Step 4: Deploy on server**

```bash
cd /opt/StreamCap
git pull
docker compose -p streamcap-f up -d --build
```

- [ ] **Step 5: Server verification (user)**

1. Watch logs: `docker logs streamcap-f-streamcap-1 --tail 50 -f`.
2. Record a live TikTok stream through a full live that previously disconnected (e.g. ayami_mori). Confirm:
   - A **single file** (e.g. `..._直播回放_2026-07-31_HH-MM-SS.ts`) after `convert_to_mp4` — not 5 files.
   - No duplicate "开播"/"live started" push notifications.
   - Log shows `Reconnecting segment 001: ...` during the live if a drop occurs, and `Merged N segments into ...` at the end.
3. If a merge fails, the raw `.ts` segments remain (verify they are not deleted).

- [ ] **Step 6: Record any issues found and iterate**

If the server test surfaces a bug, fix it in a follow-up commit and re-run Steps 4-5.
