"""
High-Speed HLS Stream Accelerator
Concurrently prefetches encrypted HLS segments in RAM using an asynchronous connection pool,
serving a zero-latency local proxy to ffmpeg so browser downloads achieve maximum network throughput.
"""

import os
import re
import math
import uuid
import asyncio
import logging
import urllib.parse
from typing import Optional, Dict, List, Tuple
import aiohttp

logger = logging.getLogger("hls_accelerator")

def get_loopback_base() -> str:
    port = os.environ.get("PORT", "7860")
    return f"http://127.0.0.1:{port}"

class HLSSession:
    def __init__(
        self,
        sid: str,
        vid_m3u8: str,
        aud_m3u8: Optional[str],
        key_bytes: Optional[bytes],
        vid_segments: List[str],
        aud_segments: List[str],
        client: aiohttp.ClientSession,
        prefetch_ahead: int = 24,
        init_bytes: Optional[bytes] = None,
    ):
        self.sid = sid
        self.vid_m3u8 = vid_m3u8
        self.aud_m3u8 = aud_m3u8
        self.key_bytes = key_bytes
        self.init_bytes = init_bytes
        self.vid_segments = vid_segments
        self.aud_segments = aud_segments
        self.client = client
        self.prefetch_ahead = max(8, prefetch_ahead)

        self.vid_cache: Dict[int, bytes] = {}
        self.aud_cache: Dict[int, bytes] = {}
        self.vid_tasks: Dict[int, asyncio.Task] = {}
        self.aud_tasks: Dict[int, asyncio.Task] = {}
        self.vid_sem = asyncio.Semaphore(16)
        self.aud_sem = asyncio.Semaphore(8)
        self.closed = False

    async def _fetch_segment(self, url: str, cache: Dict[int, bytes], tasks: Dict[int, asyncio.Task], idx: int, sem: asyncio.Semaphore) -> bytes:
        if self.closed:
            return b""
        for attempt in range(3):
            if self.closed:
                return b""
            try:
                async with sem:
                    if self.closed:
                        return b""
                    # Fast 3.5s timeout per 1MB chunk to prevent head-of-line stalling
                    async with self.client.get(url, timeout=aiohttp.ClientTimeout(total=3.5, connect=2.0)) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            if not self.closed and data:
                                cache[idx] = data
                            return data
                        elif resp.status in (403, 404):
                            break
            except asyncio.CancelledError:
                return b""
            except Exception as ex:
                if attempt < 2 and not self.closed:
                    await asyncio.sleep(0.15 * (attempt + 1))
                else:
                    logger.warning(f"Error fetching segment {idx} for session {self.sid}: {ex}")
            finally:
                if attempt == 2 or idx in cache or self.closed:
                    tasks.pop(idx, None)
        return b""

    def trigger_prefetch(self, start_idx: int, is_video: bool = True):
        if self.closed:
            return
        segs = self.vid_segments if is_video else self.aud_segments
        cache = self.vid_cache if is_video else self.aud_cache
        tasks = self.vid_tasks if is_video else self.aud_tasks
        sem = self.vid_sem if is_video else self.aud_sem

        limit = min(start_idx + self.prefetch_ahead, len(segs))
        for i in range(start_idx, limit):
            if i not in cache and i not in tasks:
                tasks[i] = asyncio.create_task(self._fetch_segment(segs[i], cache, tasks, i, sem))

    async def get_segment(self, idx: int, is_video: bool = True) -> bytes:
        if self.closed:
            return b""
        self.trigger_prefetch(idx, is_video)
        cache = self.vid_cache if is_video else self.aud_cache
        tasks = self.vid_tasks if is_video else self.aud_tasks
        segs = self.vid_segments if is_video else self.aud_segments
        sem = self.vid_sem if is_video else self.aud_sem

        if idx in cache:
            return cache.pop(idx)
        elif idx in tasks:
            try:
                res = await tasks[idx]
                cache.pop(idx, None)
                return res
            except asyncio.CancelledError:
                return b""
        elif idx < len(segs):
            return await self._fetch_segment(segs[idx], cache, tasks, idx, sem)
        return b""

    async def close(self):
        self.closed = True
        tasks = list(self.vid_tasks.values()) + list(self.aud_tasks.values())
        for t in tasks:
            if not t.done():
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.vid_tasks.clear()
        self.aud_tasks.clear()
        self.vid_cache.clear()
        self.aud_cache.clear()
        try:
            await self.client.close()
        except Exception:
            pass


class HLSAcceleratorManager:
    def __init__(self):
        self.sessions: Dict[str, HLSSession] = {}

    def get_session(self, sid: str) -> Optional[HLSSession]:
        return self.sessions.get(sid)

    async def create_session(
        self,
        master_url: str,
        http_headers: Dict[str, str],
        quality: str = "best",
        audio_lang: str = "eng",
        preview_seconds: Optional[int] = None,
        concurrency: int = 32,
    ) -> Tuple[str, bool, Optional[HLSSession]]:
        """
        Parses master playlist, resolves target video & audio streams,
        and sets up a concurrent prefetching session.
        Returns: (session_id, has_audio, session)
        """
        sid = uuid.uuid4().hex[:12]
        connector = aiohttp.TCPConnector(limit=concurrency + 10, ttl_dns_cache=300)
        client = aiohttp.ClientSession(headers=http_headers, connector=connector)

        try:
            # 1. Fetch master playlist
            async with client.get(master_url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    raise RuntimeError(f"Failed to fetch master playlist: HTTP {r.status}")
                master_text = await r.text()

            # 2. Parse video renditions and audio tracks
            lines = master_text.splitlines()
            video_variants: List[Tuple[int, str]] = []
            audio_tracks: List[Dict[str, str]] = []

            curr_stream_inf = None
            is_master = False
            for line in lines:
                line_str = line.strip()
                if not line_str:
                    continue
                if line_str.startswith("#EXT-X-STREAM-INF:"):
                    curr_stream_inf = line_str
                    is_master = True
                elif curr_stream_inf and not line_str.startswith("#"):
                    res_match = re.search(r"RESOLUTION=(\d+)x(\d+)", curr_stream_inf)
                    height = int(res_match.group(2)) if res_match else 720
                    full_sub_url = urllib.parse.urljoin(master_url, line_str)
                    video_variants.append((height, full_sub_url))
                    curr_stream_inf = None
                elif line_str.startswith("#EXT-X-MEDIA:TYPE=AUDIO"):
                    is_master = True
                    attrs = {}
                    for match in re.finditer(r'([A-Z\-]+)=(?:"([^"]*)"|([^,]*))', line_str):
                        k = match.group(1)
                        v = match.group(2) if match.group(2) is not None else match.group(3)
                        attrs[k] = v
                    audio_tracks.append(attrs)

            if not video_variants:
                # If master_url points directly to a single rendition/media playlist
                video_variants = [(1080, master_url)]

            video_variants.sort(key=lambda x: x[0], reverse=True)
            chosen_vid_url = video_variants[0][1]
            if quality == "1080p":
                matched = [v for v in video_variants if v[0] == 1080]
                if matched:
                    chosen_vid_url = matched[0][1]
            elif quality == "720p":
                matched = [v for v in video_variants if v[0] == 720]
                if matched:
                    chosen_vid_url = matched[0][1]
            elif quality == "480p":
                matched = [v for v in video_variants if v[0] <= 480]
                if matched:
                    chosen_vid_url = matched[0][1]

            chosen_aud_url = None
            if audio_tracks:
                lang_clean = audio_lang.lower().strip()
                target_track = None
                for t in audio_tracks:
                    if t.get("LANGUAGE", "").lower() == lang_clean or t.get("NAME", "").lower() == lang_clean:
                        target_track = t
                        break
                if not target_track:
                    for t in audio_tracks:
                        if t.get("DEFAULT", "").upper() == "YES":
                            target_track = t
                            break
                    if not target_track:
                        target_track = audio_tracks[0]
                raw_aud_uri = target_track.get("URI")
                if raw_aud_uri:
                    chosen_aud_url = urllib.parse.urljoin(master_url, raw_aud_uri)

            # 3. Fetch rendition playlists
            if chosen_vid_url == master_url:
                raw_vid_text = master_text
            else:
                async with client.get(chosen_vid_url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    raw_vid_text = await r.text()

            raw_aud_text = None
            if chosen_aud_url:
                async with client.get(chosen_aud_url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    raw_aud_text = await r.text()

            # 4. Fetch encryption key or init segment if referenced
            key_bytes = None
            key_match = re.search(r'#EXT-X-KEY:METHOD=AES-128,URI="([^"]+)"', raw_vid_text)
            if key_match:
                key_uri = key_match.group(1)
                key_url = urllib.parse.urljoin(chosen_vid_url, key_uri)
                try:
                    async with client.get(key_url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                        if r.status == 200:
                            key_bytes = await r.read()
                except Exception as e:
                    logger.warning(f"Could not fetch key: {e}")

            init_bytes = None
            map_match = re.search(r'#EXT-X-MAP:URI="([^"]+)"', raw_vid_text)
            if map_match:
                map_uri = map_match.group(1)
                map_url = urllib.parse.urljoin(chosen_vid_url, map_uri)
                try:
                    async with client.get(map_url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                        if r.status == 200:
                            init_bytes = await r.read()
                except Exception as e:
                    logger.warning(f"Could not fetch init map segment: {e}")

            # 5. Build rewritten playlists
            vid_segs: List[str] = []
            aud_segs: List[str] = []

            max_segs = None
            if preview_seconds and preview_seconds > 0:
                max_segs = max(1, math.ceil(preview_seconds / 4.0))

            loopback = get_loopback_base()
            rewritten_vid_lines = []
            v_idx = 0
            for l in raw_vid_text.splitlines():
                line_s = l.strip()
                if not line_s:
                    continue
                if line_s.startswith("#EXT-X-KEY:METHOD=AES-128"):
                    iv_match = re.search(r"IV=([0-9a-zA-Zx]+)", line_s)
                    iv_part = f",IV={iv_match.group(1)}" if iv_match else ""
                    rewritten_vid_lines.append(f'#EXT-X-KEY:METHOD=AES-128,URI="{loopback}/api/hls_accel/{sid}/key"{iv_part}')
                elif line_s.startswith("#EXT-X-MAP:"):
                    map_m = re.search(r'URI="([^"]+)"', line_s)
                    if map_m:
                        if init_bytes:
                            rewritten_vid_lines.append(f'#EXT-X-MAP:URI="{loopback}/api/hls_accel/{sid}/init.mp4"')
                        else:
                            abs_map = urllib.parse.urljoin(chosen_vid_url, map_m.group(1))
                            rewritten_vid_lines.append(f'#EXT-X-MAP:URI="{abs_map}"')
                    else:
                        rewritten_vid_lines.append(line_s)
                elif not line_s.startswith("#"):
                    if max_segs is None or v_idx < max_segs:
                        seg_url = urllib.parse.urljoin(chosen_vid_url, line_s)
                        vid_segs.append(seg_url)
                        rewritten_vid_lines.append(f"{loopback}/api/hls_accel/{sid}/v/{v_idx}.ts")
                        v_idx += 1
                elif line_s.startswith("#EXT-X-ENDLIST"):
                    continue
                else:
                    rewritten_vid_lines.append(line_s)
            rewritten_vid_lines.append("#EXT-X-ENDLIST")

            rewritten_aud_lines = []
            if raw_aud_text:
                a_idx = 0
                for l in raw_aud_text.splitlines():
                    line_s = l.strip()
                    if not line_s:
                        continue
                    if line_s.startswith("#EXT-X-KEY:METHOD=AES-128"):
                        iv_match = re.search(r"IV=([0-9a-zA-Zx]+)", line_s)
                        iv_part = f",IV={iv_match.group(1)}" if iv_match else ""
                        rewritten_aud_lines.append(f'#EXT-X-KEY:METHOD=AES-128,URI="{loopback}/api/hls_accel/{sid}/key"{iv_part}')
                    elif line_s.startswith("#EXT-X-MAP:"):
                        map_m = re.search(r'URI="([^"]+)"', line_s)
                        if map_m:
                            abs_map = urllib.parse.urljoin(chosen_aud_url, map_m.group(1))
                            rewritten_aud_lines.append(f'#EXT-X-MAP:URI="{abs_map}"')
                        else:
                            rewritten_aud_lines.append(line_s)
                    elif not line_s.startswith("#"):
                        if max_segs is None or a_idx < max_segs:
                            seg_url = urllib.parse.urljoin(chosen_aud_url, line_s)
                            aud_segs.append(seg_url)
                            rewritten_aud_lines.append(f"{loopback}/api/hls_accel/{sid}/a/{a_idx}.ts")
                            a_idx += 1
                    elif line_s.startswith("#EXT-X-ENDLIST"):
                        continue
                    else:
                        rewritten_aud_lines.append(line_s)
                rewritten_aud_lines.append("#EXT-X-ENDLIST")

            session = HLSSession(
                sid=sid,
                vid_m3u8="\n".join(rewritten_vid_lines),
                aud_m3u8="\n".join(rewritten_aud_lines) if rewritten_aud_lines else None,
                key_bytes=key_bytes,
                vid_segments=vid_segs,
                aud_segments=aud_segs,
                client=client,
                prefetch_ahead=concurrency,
                init_bytes=init_bytes,
            )
            self.sessions[sid] = session

            session.trigger_prefetch(0, True)
            if aud_segs:
                session.trigger_prefetch(0, False)

            # Pre-buffer initial segments before ffmpeg starts reading so the stream has a warm cache
            try:
                pre_tasks = []
                for idx in range(min(3, len(vid_segs))):
                    if idx in session.vid_tasks:
                        pre_tasks.append(session.vid_tasks[idx])
                if aud_segs:
                    for idx in range(min(3, len(aud_segs))):
                        if idx in session.aud_tasks:
                            pre_tasks.append(session.aud_tasks[idx])
                if pre_tasks:
                    await asyncio.wait(pre_tasks, timeout=1.5)
            except Exception:
                pass

            has_audio = bool(aud_segs)
            return sid, has_audio, session

        except Exception as ex:
            await client.close()
            raise ex

    async def remove_session(self, sid: str):
        session = self.sessions.pop(sid, None)
        if session:
            await session.close()


accelerator_manager = HLSAcceleratorManager()
