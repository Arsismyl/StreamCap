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
        # If the only non-empty segment is not the canonical first path (e.g. the
        # first pass produced an empty file but a reconnect segment has content),
        # move it onto the canonical first path so downstream convert/script
        # always sees the content at segment_paths[0].
        if valid and valid[0] != segment_paths[0]:
            os.replace(valid[0], segment_paths[0])
            return segment_paths[0]
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
