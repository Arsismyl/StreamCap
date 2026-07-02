# YouTube Recording Empty Files Fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Pass `Referer` and `Cookie` headers to FFmpeg when recording YouTube live streams so that `googlevideo.com` CDN serves `.ts` segments.

**Architecture:** Single-file change to `app/core/recording/stream_manager.py`. Add YouTube to the `get_headers_params()` static method, and append the configured YouTube cookie to the FFmpeg `-headers` parameter in `start_recording()`.

**Tech Stack:** Python, FFmpeg, HLS

---

### Task 1: Add YouTube headers support in stream_manager.py

**Files:**
- Modify: `app/core/recording/stream_manager.py` (two locations)

- [ ] **Step 1: Add YouTube referer to get_headers_params()**

In `get_headers_params()` (line 651), add `"youtube"` to the `record_headers` dict:

```python
@staticmethod
def get_headers_params(live_url, platform_key):
    live_domain = "/".join(live_url.split("/")[0:3])
    record_headers = {
        "pandalive": "origin:https://www.pandalive.co.kr",
        "winktv": "origin:https://www.winktv.co.kr",
        "popkontv": "origin:https://www.popkontv.com",
        "flextv": "origin:https://www.flextv.co.kr",
        "qiandurebo": "referer:https://qiandurebo.com",
        "17live": "referer:https://17.live/en/live/6302408",
        "lang": "referer:https://www.lang.live",
        "shopee": "origin:" + live_domain,
        "blued": "referer:https://app.blued.cn",
        "xindongrebo": "referer:https://xcqrkj.com",
        "youtube": "referer:https://www.youtube.com",
    }
    return record_headers.get(platform_key)
```

- [ ] **Step 2: Add cookie header for YouTube in start_recording()**

In `start_recording()`, around line 309-317, modify the FFmpeg builder call to append the Cookie header when platform is YouTube and cookies exist:

```python
else:
    header_params = self.get_headers_params(record_url, self.platform_key)
    if self.cookies and self.platform_key == "youtube":
        cookie_header = f"Cookie: {self.cookies}"
        header_params = f"{header_params}\r\n{cookie_header}" if header_params else cookie_header

    ffmpeg_builder = ffmpeg_builders.create_builder(
        self.save_format,
        record_url=record_url,
        proxy=self.proxy,
        segment_record=self.segment_record,
        segment_time=self.segment_time,
        full_path=save_path,
        headers=header_params,
    )
```

- [ ] **Step 3: Validate syntax**

```bash
cd /path/to/StreamCap
python -c "import ast; ast.parse(open('app/core/recording/stream_manager.py').read()); print('OK')"
```
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add app/core/recording/stream_manager.py
git commit -m "fix: pass Referer and Cookie headers to FFmpeg for YouTube recording

YouTube's CDN (googlevideo.com) requires Referer and authentication
cookies to serve HLS segments. Add YouTube to get_headers_params()
and forward the configured cookies via FFmpeg's -headers parameter.
```
