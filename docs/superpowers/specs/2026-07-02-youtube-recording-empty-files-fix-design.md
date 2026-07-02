# Fix: YouTube Recording Produces Empty Files

## Problem

When recording YouTube live streams, StreamCap:
1. Successfully fetches stream metadata via streamget
2. Creates the output directory and file
3. Shows "Recording in Progress" in UI
4. But the output file remains empty (0 bytes)

The folder and file are created by FFmpeg on startup, but no media data is ever written.

## Root Cause

FFmpeg's HLS demuxer can parse the master playlist (m3u8), but fails when downloading the individual `.ts` segments from `googlevideo.com`:

```
FFmpeg Stderr Output: [hls @ 0x561db110e640] Error when loading first segment '...googlevideo.com.../file/seg.ts'
```

YouTube's CDN requires `Referer: https://www.youtube.com` and authentication cookies to serve segments. StreamCap's `get_headers_params()` (in `stream_manager.py`) does not include YouTube, so FFmpeg starts without any custom HTTP headers.

The user's YouTube cookies are loaded from config (`self.cookies = self.settings.cookies_config.get(self.platform_key)`) and passed to streamget for metadata fetching, but **never forwarded to FFmpeg** for segment downloads.

## Solution

Add YouTube to `get_headers_params()` and pass the configured cookies to FFmpeg via `-headers`.

### Changes

**File:** `app/core/recording/stream_manager.py`

1. `get_headers_params()`: Add `"youtube"` → `"referer:https://www.youtube.com"` to the record_headers dict.

2. `start_recording()` / `start_ffmpeg()`: When platform is YouTube and cookies exist, append a `Cookie` header to the FFmpeg `-headers` parameter. The cookie string is already loaded from config at `self.cookies = self.settings.cookies_config.get(self.platform_key)` (line 40 of stream_manager.py).

### How FFmpeg Handles Headers

The builder already supports the `-headers` parameter:
```python
# base.py:99-101
if self.headers:
    command.insert(11, "-headers")
    command.insert(12, self.headers)
```

Headers are passed as raw HTTP header lines. Multiple headers are separated by `\r\n`:
```
referer:https://www.youtube.com\r\nCookie: <cookie-value>
```

### Scope

- Only affects YouTube recordings
- Other platforms (Douyin, Twitch, etc.) are unchanged
- If user has no YouTube cookies configured, only the Referer header is sent
