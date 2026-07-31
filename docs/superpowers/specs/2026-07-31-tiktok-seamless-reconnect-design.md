# Design: TikTok Seamless Reconnect (single continuous file per live)

## Problem

Recording a single TikTok live produces **multiple separate files** with visible gaps. Example from `streamget.log` (2026-07-31, ayami_mori, ~2h24m live):

| Seg | Start | End | Duration | Exit code | Cause |
|-----|-------|-----|----------|-----------|-------|
| 1 | 05:08:06 | 05:48:34 | 40m | 0 | CDN drop |
| 2 | 05:50:06 | 05:50:10 | 4s | 8 | 5XX Server Error |
| 3 | 05:50:17 | 06:17:15 | 27m | 0 | CDN drop |
| 4 | 06:18:06 | 07:11:58 | 54m | 0 | CDN drop |
| 5 | 07:12:06 | 07:12:23 | 17s | 0 | CDN drop |
| 6 | 07:14:06 | 07:32:53 | 19m | 0 | CDN drop |

6 files, ~4 minutes of live content missed across the gaps (8s to ~103s each).

## Root Cause

1. **TikTok CDN closes FLV pull connections at irregular intervals.** When it closes cleanly, FFmpeg sees EOF and exits **code=0** (treated as "normal end"). Sometimes it returns a **5XX** (exit code 8). Both have the same underlying cause: the CDN terminates the pull session, and the signed URL (`sign` parameter) is rotated, so FFmpeg's built-in `-reconnect_at_eof` cannot succeed (it retries the same now-stale URL).

2. **The app waits up to `loop_time_seconds` (120s) before restarting.** `recheck_live_status()` in `stream_manager.py` explicitly skips FLV-preferred platforms (`not self.is_flv_preferred_platform`). After FFmpeg exits, the recorder is marked finished; the next periodic `check_if_live` (every ~120s) detects the stream is still live and starts a NEW recording. Result: multi-file output with minute-scale gaps.

## Solution (two coordinated parts)

Scope: **FLV-preferred platforms only** (`tiktok`, `douyin`). All other platforms keep the current code path untouched.

### Part 1 — Fast reconnect with notification suppression

When FFmpeg exits (code 0 or 8) and the recording was NOT manually stopped, immediately re-fetch stream data. If the stream is still live, restart FFmpeg within ~2-5s with the fresh signed URL, writing to the **next segment file**. The recording state (`is_live`, `is_recording`, duration counter) stays continuous, so **no duplicate "live started" push notification** is sent and the UI duration does not reset.

If the re-fetch reports `is_live=False`, the stream genuinely ended → finalize normally (this is the only path that declares the recording complete).

### Part 2 — Segment files + lossless merge

- First segment writes to the original path (e.g. `base.ts`) — unchanged from today.
- Each reconnect writes to `base_001.ts`, `base_002.ts`, … (zero-padded, never overwriting).
- When the live truly ends (or the user stops it), merge all segments in order into the final file using `ffmpeg -f concat -safe 0 -c copy` (TS segments concatenate losslessly). The merged result replaces `base.ts`, then the existing `convert_to_mp4` logic runs unchanged.
- **If merge fails, the raw segments are kept as-is** — identical to today's multi-file output. Data is never deleted before a verified merge.

## Design Details

### State additions (`LiveStreamRecorder.__init__`)

- `self.segment_paths: list[str]` — files produced by this recording (for merge).
- `self.reconnect_failures: int` — consecutive failed reconnect attempts.
- `self.reconnect_max_failures = 3` — give up reconnecting after 3 consecutive failures (prevents infinite loop on a broken/dead stream).
- `self.reconnect_delay` — seconds between reconnect attempts; read from the existing `recording_retry_interval` user setting (default 5, clamped 1..60), so it matches the existing 5XX auto-retry behavior.

### Reconnect loop (in `start_ffmpeg`)

Restructure `start_ffmpeg` so the "spawn → monitor → collect exit" logic runs inside a loop:

```
loop:
    spawn ffmpeg (fresh signed URL, next segment path)
    monitor until stop-requested / process exits
    collect exit_code + stderr

    if not FLV-preferred platform:      → break (today's behavior)
    if should_stop / force_stop / !recording_enabled: → break
    fetch stream info (fresh)
    if fetch failed or is_live=False:    → reconnect_failures++, if < max: sleep, retry fetch; else break
    if is_live=True with new URL:        → reset reconnect_failures, next segment, continue loop
```

Key behavior preserved from today's code (moved into the single-pass path):
- Graceful stop (SIGINT → wait → force kill on timeout).
- Non-safe exit code → `_handle_recording_error`.
- Safe exit code → `_handle_recording_finished` (only when NOT reconnecting).
- `recheck_live_status()`, `convert_to_mp4`, `custom_script_execute` run only on the FINAL segment (after the loop), not per segment.
- The existing one-shot 5XX auto-retry (`_retried`) is superseded by the reconnect loop for FLV platforms; it remains for non-FLV platforms.

### Merge step (after the loop, on final segment)

```
if len(segment_paths) > 1:
    write concat list file (segment paths, absolute)
    run: ffmpeg -f concat -safe 0 -i <list> -c copy -y <base>.merged<ext>
    if success and merged file exists and size > 0:
        rename <base>.merged<ext> → <base><ext>   (atomically replaces the first segment)
        delete segment files base_*.ts
        log success
    else:
        keep all segments (fallback to today's multi-file output), log warning
```

The merge output replaces `base.ts` so downstream `convert_to_mp4` / custom script logic is unchanged. Empty or tiny "segments" (e.g. the 17s one) are included in the merge — TS concat tolerates them; they only contain what was actually broadcast.

### Notification suppression

The reconnect path calls `fetch_stream()` directly and manages `recording.is_live` itself — it never goes through `record_manager.check_if_live`, so the "live started" push notification and desktop notification are **not** re-sent. `recording.is_live` remains `True` throughout.

## Error Handling

| Failure point | Behavior |
|---------------|----------|
| Re-fetch raises (network) | Treated as a failed attempt; retry up to `reconnect_max_failures` (3), then finalize with recorded segments |
| Re-fetch returns `is_live=False` | Stream ended → finalize + merge normally |
| FFmpeg exit non-safe code during reconnect | Still counts toward reconnect loop; recorded segment preserved |
| Merge command fails | Keep raw segments; log warning; no data loss |
| Merge succeeds | Replace base + delete segments; existing convert/script run on merged file |
| App stopping / user stop | `should_stop`/`force_stop` breaks the loop immediately; finalize + merge whatever exists |

**Guarantee:** no error path is worse than today's behavior (multi-file output). Reconnect only ever *reduces* gaps and merges files; it never deletes recorded content before a verified merge.

## Scope / Impact

- Affected: `app/core/recording/stream_manager.py` (`LiveStreamRecorder`). Possibly a small helper for the merge.
- Only `tiktok` / `douyin` recordings change behavior. HLS platforms (YouTube, TwitCasting, Twitch, etc.), direct-download platforms, and segmented-recording (`segment_record`) configs are untouched.
- No settings/UI changes required.

## Testing

Local verification (static + synthetic):
1. Python syntax / import check: `python -m py_compile app/core/recording/stream_manager.py`.
2. Trace the reconnect loop with mocked `fetch_stream` returning live→live→not-live, assert 3 segments produced and merged.
3. Verify non-FLV path is byte-for-byte equivalent to today's flow (single pass, no merge).

Server verification (user):
1. Deploy via `cd /opt/StreamCap && git pull && docker compose -p streamcap-f up -d --build`.
2. Record a live TikTok streamer through a full live; confirm a **single file** with no multi-minute gaps, and no duplicate "live started" notifications.
3. Force a disconnect (if possible) and confirm reconnect within ~3s and segments merge at the end.
