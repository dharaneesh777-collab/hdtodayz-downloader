"""
Background Video Download Engine
Manages asynchronous download tasks, progress tracking, and SSE broadcasting using yt-dlp and ffmpeg.
"""

import os
import re
import time
import uuid
import shutil
import asyncio
import logging
import threading
import subprocess
import urllib.parse
from pathlib import Path
from typing import Dict, Any, Optional, List, Set

from app.extractor import extract_vixsrc_stream, check_stream_available, search_media

logger = logging.getLogger("hdtoday.downloader")
logging.basicConfig(level=logging.INFO)

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "storage_config.json"
DEFAULT_DOWNLOADS_DIR = Path.home() / "Downloads" / "HDTodayz"


def get_downloads_dir() -> Path:
    """Retrieve the active local device downloads directory, defaulting to ~/Downloads/HDTodayz."""
    if CONFIG_FILE.exists():
        try:
            import json
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                custom_dir = data.get("download_dir")
                if custom_dir:
                    p = Path(custom_dir).resolve()
                    p.mkdir(parents=True, exist_ok=True)
                    return p
        except Exception as e:
            logger.warning(f"Could not load custom storage path: {e}")

    try:
        DEFAULT_DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        return DEFAULT_DOWNLOADS_DIR
    except Exception:
        # Fallback to BASE_DIR / "downloads" if permissions fail
        fallback = BASE_DIR / "downloads"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def set_downloads_dir(path_str: str) -> Path:
    """Update and persist the active local device downloads directory."""
    import json
    p = Path(path_str).resolve()
    p.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"download_dir": str(p)}, f, indent=2)
    return p


def migrate_legacy_downloads():
    """Migrate any previously downloaded media from the scratch folder to the local device storage."""
    legacy_dir = BASE_DIR / "downloads"
    target_dir = get_downloads_dir()
    if legacy_dir.exists() and legacy_dir.resolve() != target_dir.resolve():
        migrated_count = 0
        for item in list(legacy_dir.iterdir()):
            if item.is_file() and item.suffix.lower() in (".mp4", ".mkv", ".webm", ".avi", ".ts"):
                if not item.name.endswith(".part") and not item.name.endswith(".ytdl"):
                    target_file = target_dir / item.name
                    if not target_file.exists():
                        try:
                            shutil.move(str(item), str(target_file))
                            migrated_count += 1
                            logger.info(f"Migrated completed file to local storage: {target_file}")
                        except Exception as e:
                            logger.error(f"Failed to migrate {item.name}: {e}")
        if migrated_count > 0:
            logger.info(f"Successfully migrated {migrated_count} video files directly into {target_dir}")


DOWNLOADS_DIR = get_downloads_dir()
SUBTITLES_CACHE_DIR = BASE_DIR / "cache" / "subtitles"
SUBTITLES_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def get_subtitles_cache_dir() -> Path:
    """Retrieve internal app cache directory for WebVTT and temporary SRT subtitles."""
    SUBTITLES_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return SUBTITLES_CACHE_DIR


def cleanup_job_junk_files(filename: str, target_dir: Optional[Path] = None, clean_cache: bool = False):
    """Clean up any partial fragments, ytdl metadata, and temporary files for a given video."""
    if target_dir is None:
        target_dir = get_downloads_dir()
    stem = Path(filename).stem
    if not target_dir.exists():
        return

    # Sweep any matching partial files, ytdl metadata, or loose srt/vtt in target_dir
    for f in list(target_dir.iterdir()):
        if not f.is_file():
            continue
        fname = f.name
        if fname.startswith(stem):
            lower = fname.lower()
            if (
                ".part" in lower
                or ".ytdl" in lower
                or lower.endswith(".temp")
                or ".part-frag" in lower
                or ".unknown_video" in lower
                or lower.endswith(".srt")
                or lower.endswith(".vtt")
            ):
                try:
                    f.unlink(missing_ok=True)
                    logger.info(f"Cleaned up junk/temp file: {fname}")
                except Exception as e:
                    logger.warning(f"Could not delete junk file {fname}: {e}")

    # If clean_cache is True (e.g. on cancel or failure), also sweep subtitles cache for this job
    if clean_cache:
        cache_dir = get_subtitles_cache_dir()
        if cache_dir.exists():
            for cf in list(cache_dir.glob(f"{stem}*")):
                try:
                    cf.unlink(missing_ok=True)
                except Exception:
                    pass


def cleanup_all_junk_files(target_dir: Optional[Path] = None) -> int:
    """Sweep and remove all orphaned .part, .part-Frag*, .ytdl, and loose .srt/.vtt files in the download directory."""
    if target_dir is None:
        target_dir = get_downloads_dir()
    cleaned = 0
    if not target_dir.exists():
        return 0

    cache_dir = get_subtitles_cache_dir()

    for f in list(target_dir.iterdir()):
        if not f.is_file():
            continue
        lower = f.name.lower()
        is_junk = (
            ".part" in lower
            or ".ytdl" in lower
            or lower.endswith(".temp")
            or ".part-frag" in lower
            or ".unknown_video" in lower
            or lower.endswith(".srt")
            or lower.endswith(".vtt")
        )
        if is_junk:
            # If it's a vtt, ensure a copy exists in cache_dir so web player can still read it
            if lower.endswith(".vtt") and not (cache_dir / f.name).exists():
                try:
                    shutil.copy2(str(f), str(cache_dir / f.name))
                except Exception:
                    pass
            try:
                f.unlink(missing_ok=True)
                cleaned += 1
            except Exception as e:
                logger.warning(f"Failed to delete {f.name}: {e}")
    if cleaned > 0:
        logger.info(f"Cleaned up {cleaned} orphaned junk/loose subtitle files from {target_dir}")
    return cleaned


def vtt_to_srt(vtt_text: str) -> str:
    """Convert WebVTT subtitle text to standard SubRip SRT format."""
    lines = vtt_text.splitlines()
    srt_lines = []
    sub_index = 1
    i = 0
    # Skip header
    while i < len(lines) and not ("-->" in lines[i]):
        i += 1

    def fix_ts(ts: str) -> str:
        ts = ts.strip().split(" ")[0]
        parts = ts.split(":")
        if len(parts) == 2:
            ts = "00:" + ts
        return ts.replace(".", ",")

    while i < len(lines):
        line = lines[i].strip()
        if "-->" in line:
            try:
                start_raw, end_raw = line.split("-->")
                start_srt = fix_ts(start_raw)
                end_srt = fix_ts(end_raw)
                srt_lines.append(str(sub_index))
                srt_lines.append(f"{start_srt} --> {end_srt}")
                sub_index += 1
                i += 1
                while i < len(lines) and lines[i].strip():
                    cue_line = lines[i].strip()
                    cue_line = re.sub(r"<[^>]+>", "", cue_line)
                    if cue_line:
                        srt_lines.append(cue_line)
                    i += 1
                srt_lines.append("")
            except Exception:
                pass
        i += 1
    return "\n".join(srt_lines)


def download_and_extract_subtitles(
    subtitles_data: List[Dict[str, Any]],
    base_stem: str,
    preferred_lang: str = "eng",
    output_dir: Optional[Path] = None,
) -> List[Dict[str, str]]:
    """
    Download VTT subtitles from HLS playlist URIs, convert to SRT,
    and save them into the internal subtitles cache (NEVER loose in the download directory).
    """
    from app.extractor import DEFAULT_HEADERS
    if not subtitles_data or preferred_lang == "none":
        return []

    cache_dir = output_dir or get_subtitles_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)

    headers = {
        "Referer": "https://vixsrc.to/",
        "User-Agent": DEFAULT_HEADERS["User-Agent"],
    }

    pref = preferred_lang.lower().strip()
    selected = []
    if pref == "all":
        selected = list(subtitles_data)
    else:
        for s in subtitles_data:
            lang = s.get("language", "").lower()
            name = s.get("name", "").lower()
            if pref in lang or lang in pref or pref in name:
                selected.append(s)

        if not selected:
            for s in subtitles_data:
                if "eng" in s.get("language", "").lower() or "english" in s.get("name", "").lower():
                    selected.append(s)
                    break

    selected.sort(key=lambda x: (not x.get("forced", False), "eng" in x.get("language", "").lower()), reverse=True)

    results = []
    for sub in selected[:2]:
        uri = sub.get("uri")
        if not uri:
            continue

        try:
            req = urllib.request.Request(uri, headers=headers)
            playlist_text = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", errors="replace")

            vtt_urls = []
            for line in playlist_text.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    full_u = line if line.startswith("http") else urllib.parse.urljoin(uri, line)
                    vtt_urls.append(full_u)

            if not vtt_urls:
                continue

            full_vtt = ""
            for v_url in vtt_urls:
                r = urllib.request.Request(v_url, headers=headers)
                part = urllib.request.urlopen(r, timeout=10).read().decode("utf-8", errors="replace")
                full_vtt += part + "\n"

            if not full_vtt.strip():
                continue

            srt_content = vtt_to_srt(full_vtt)
            clean_lang = sub.get("language", "und")

            # Save clean canonical srt and vtt in internal cache ONLY
            base_srt_path = cache_dir / f"{base_stem}.srt"
            base_vtt_path = cache_dir / f"{base_stem}.vtt"
            lang_srt_path = cache_dir / f"{base_stem}.{clean_lang}.srt"
            lang_vtt_path = cache_dir / f"{base_stem}.{clean_lang}.vtt"

            base_srt_path.write_text(srt_content, encoding="utf-8")
            base_vtt_path.write_text(full_vtt, encoding="utf-8")
            lang_srt_path.write_text(srt_content, encoding="utf-8")
            lang_vtt_path.write_text(full_vtt, encoding="utf-8")

            results.append({
                "language": clean_lang,
                "name": sub.get("name", clean_lang),
                "srt_path": str(base_srt_path),
                "vtt_path": str(base_vtt_path),
                "vtt_filename": f"{base_stem}.vtt",
            })
            logger.info(f"Saved subtitle track in cache for {base_stem}: {sub.get('name', clean_lang)}")

        except Exception as e:
            logger.warning(f"Failed to fetch subtitle {sub.get('name')}: {e}")

    return results


def embed_subtitles_in_mp4(mp4_path: Path, srt_path: Path, lang: str = "eng", title: str = "English") -> bool:
    """Embed subtitle track into mp4 container using ffmpeg mov_text with no re-encoding."""
    if not mp4_path.exists() or not srt_path.exists():
        return False
    temp_output = mp4_path.with_suffix(".sub_embedded.mp4")
    cmd = [
        "ffmpeg", "-y",
        "-i", str(mp4_path),
        "-i", str(srt_path),
        "-c:v", "copy",
        "-c:a", "copy",
        "-c:s", "mov_text",
        "-metadata:s:s:0", f"language={lang}",
        "-metadata:s:s:0", f"title={title}",
        str(temp_output)
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if res.returncode == 0 and temp_output.exists() and temp_output.stat().st_size > 1000:
            temp_output.replace(mp4_path)
            logger.info(f"Embedded subtitle track into {mp4_path.name}")
            return True
        else:
            if temp_output.exists():
                temp_output.unlink(missing_ok=True)
            logger.warning(f"ffmpeg subtitle embedding failed: {res.stderr[:200] if res.stderr else ''}")
            return False
    except Exception as e:
        if temp_output.exists():
            temp_output.unlink(missing_ok=True)
        logger.warning(f"Exception during subtitle embedding: {e}")
        return False


def sanitize_filename(name: str) -> str:
    """Sanitize string to be a safe filename across operating systems."""
    # Replace invalid Windows/POSIX filename characters
    clean = re.sub(r'[\\/*?:"<>|]', "", name)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean or "video"


class DownloadJob:
    def __init__(
        self,
        job_id: str,
        tmdb_id: int,
        title: str,
        media_type: str = "movie",
        year: str = "",
        season: Optional[int] = None,
        episode: Optional[int] = None,
        episode_title: Optional[str] = None,
        quality: str = "best",
        audio_lang: str = "eng",
        sub_lang: str = "eng",
        subtitles_enabled: bool = True,
        poster_url: str = "",
        preview_seconds: Optional[int] = None,
        threads: int = 16,
    ):
        self.job_id = job_id
        self.tmdb_id = tmdb_id
        self.title = title
        self.media_type = media_type
        self.year = year
        self.season = season
        self.episode = episode
        self.episode_title = episode_title
        self.quality = quality
        self.audio_lang = audio_lang
        self.sub_lang = sub_lang
        self.subtitles_enabled = subtitles_enabled
        self.subtitles_downloaded: List[Dict[str, str]] = []
        self.poster_url = poster_url
        self.preview_seconds = preview_seconds
        self.threads = threads

        self.status = "queued"  # queued, extracting, downloading, merging, completed, failed, cancelled
        self.progress = 0.0
        self.speed = "0 KiB/s"
        self.eta = "--:--"
        self.downloaded_bytes = 0
        self.total_bytes = 0
        self.total_size_str = ""
        self.error: Optional[str] = None

        # Determine target filename
        prev_tag = f"_preview{self.preview_seconds}s" if self.preview_seconds else ""
        if media_type == "tv" and season and episode:
            ep_suffix = f"S{season:02d}E{episode:02d}"
            if episode_title:
                clean_ep = sanitize_filename(episode_title)
                self.filename = f"{sanitize_filename(title)}.{ep_suffix}.{clean_ep}{prev_tag}.mp4"
            else:
                self.filename = f"{sanitize_filename(title)}.{ep_suffix}{prev_tag}.mp4"
        else:
            yr_suffix = f"_{year}" if year else ""
            self.filename = f"{sanitize_filename(title)}{yr_suffix}{prev_tag}.mp4"

        target_dir = get_downloads_dir()
        self.filepath = str(target_dir / self.filename)
        self.file_size = 0
        self.created_at = time.time()
        self.completed_at: Optional[float] = None
        self.process: Optional[subprocess.Popen] = None
        self.listeners: Set[asyncio.Queue] = set()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "tmdb_id": self.tmdb_id,
            "title": self.title,
            "media_type": self.media_type,
            "year": self.year,
            "season": self.season,
            "episode": self.episode,
            "episode_title": self.episode_title,
            "quality": self.quality,
            "audio_lang": self.audio_lang,
            "sub_lang": self.sub_lang,
            "subtitles_enabled": self.subtitles_enabled,
            "subtitles": self.subtitles_downloaded,
            "poster_url": self.poster_url,
            "threads": self.threads,
            "status": self.status,
            "progress": round(self.progress, 1),
            "speed": self.speed,
            "eta": self.eta,
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "total_size_str": self.total_size_str,
            "filename": self.filename,
            "filepath": self.filepath,
            "file_size": self.file_size,
            "download_url": f"/api/files/{urllib.parse.quote(self.filename)}?download=1" if self.status == "completed" else None,
            "stream_url": f"/api/files/{urllib.parse.quote(self.filename)}" if self.status == "completed" else None,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "error": self.error,
        }

    def broadcast(self):
        """Notify any listening SSE queues of state update."""
        data = self.to_dict()
        for q in list(self.listeners):
            try:
                q.put_nowait(data)
            except Exception:
                pass


class DownloadManager:
    def __init__(self, max_concurrent: int = 2):
        self.jobs: Dict[str, DownloadJob] = {}
        self.lock = threading.Lock()
        self.max_concurrent = max_concurrent

    def _schedule_next(self):
        """Check active jobs count and trigger the next queued job if capacity permits."""
        with self.lock:
            # Count currently active jobs (extracting, downloading, merging)
            active_count = sum(
                1 for j in self.jobs.values()
                if j.status in ("extracting", "downloading", "merging")
            )
            slots_available = max(0, self.max_concurrent - active_count)
            if slots_available <= 0:
                return

            # Find next queued jobs (FIFO order by created_at)
            queued_jobs = [
                j for j in self.jobs.values()
                if j.status == "queued"
            ]
            queued_jobs.sort(key=lambda j: j.created_at)

            for job in queued_jobs[:slots_available]:
                # Mark as extracting so another schedule cycle doesn't start it again
                job.status = "extracting"
                job.broadcast()
                t = threading.Thread(target=self._run_job, args=(job,), daemon=True)
                t.start()

    def create_job(
        self,
        tmdb_id: int,
        title: str,
        media_type: str = "movie",
        year: str = "",
        season: Optional[int] = None,
        episode: Optional[int] = None,
        episode_title: Optional[str] = None,
        quality: str = "best",
        audio_lang: str = "eng",
        sub_lang: str = "eng",
        subtitles_enabled: bool = True,
        poster_url: str = "",
        preview_seconds: Optional[int] = None,
        threads: int = 16,
    ) -> DownloadJob:
        job_id = str(uuid.uuid4())[:8]
        job = DownloadJob(
            job_id=job_id,
            tmdb_id=tmdb_id,
            title=title,
            media_type=media_type,
            year=year,
            season=season,
            episode=episode,
            episode_title=episode_title,
            quality=quality,
            audio_lang=audio_lang,
            sub_lang=sub_lang,
            subtitles_enabled=subtitles_enabled,
            poster_url=poster_url,
            preview_seconds=preview_seconds,
            threads=threads,
        )
        with self.lock:
            self.jobs[job_id] = job

        self._schedule_next()
        return job

    def create_batch_jobs(
        self,
        tmdb_id: int,
        title: str,
        media_type: str = "tv",
        year: str = "",
        season: int = 1,
        episodes: Optional[List[Dict[str, Any]]] = None,
        quality: str = "best",
        audio_lang: str = "eng",
        sub_lang: str = "eng",
        subtitles_enabled: bool = True,
        poster_url: str = "",
        threads: int = 16,
    ) -> List[DownloadJob]:
        if episodes is None:
            episodes = []

        created_jobs = []
        with self.lock:
            for ep_data in episodes:
                job_id = str(uuid.uuid4())[:8]
                ep_num = int(ep_data.get("episode") or ep_data.get("episode_number") or 1)
                ep_title = ep_data.get("episode_title") or ep_data.get("name") or f"Episode {ep_num}"
                job = DownloadJob(
                    job_id=job_id,
                    tmdb_id=tmdb_id,
                    title=title,
                    media_type=media_type,
                    year=year,
                    season=season,
                    episode=ep_num,
                    episode_title=ep_title,
                    quality=quality,
                    audio_lang=audio_lang,
                    sub_lang=sub_lang,
                    subtitles_enabled=subtitles_enabled,
                    poster_url=poster_url,
                    preview_seconds=None,
                    threads=threads,
                )
                self.jobs[job_id] = job
                created_jobs.append(job)

        self._schedule_next()
        return created_jobs

    def get_job(self, job_id: str) -> Optional[DownloadJob]:
        return self.jobs.get(job_id)

    def list_jobs(self) -> List[Dict[str, Any]]:
        with self.lock:
            return [j.to_dict() for j in reversed(list(self.jobs.values()))]

    def cancel_job(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job:
            return False

        job.status = "cancelled"
        if job.process and job.process.poll() is None:
            try:
                if os.name == "nt":
                    # Forcefully kill process AND ALL child processes immediately on Windows
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(job.process.pid)],
                        capture_output=True,
                        timeout=5
                    )
                else:
                    job.process.kill()
            except Exception as e:
                logger.warning(f"Error terminating process tree for job {job_id}: {e}")

        # Short pause so Windows releases open file handles
        time.sleep(0.3)

        # Thorough sweep of all partial, fragment, ytdl, and cache files
        cleanup_job_junk_files(job.filename, clean_cache=True)

        job.broadcast()
        self._schedule_next()
        return True

    def delete_job(self, job_id: str, delete_file: bool = False) -> bool:
        with self.lock:
            job = self.jobs.pop(job_id, None)
        if not job:
            return False

        if job.process and job.process.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(job.process.pid)],
                        capture_output=True,
                        timeout=5
                    )
                else:
                    job.process.kill()
            except Exception:
                pass
            time.sleep(0.2)

        cleanup_job_junk_files(job.filename, clean_cache=True)

        if delete_file:
            fp = Path(job.filepath)
            if fp.exists():
                try:
                    fp.unlink()
                except Exception as e:
                    logger.error(f"Error deleting file {fp}: {e}")

        self._schedule_next()
        return True

    def _run_job(self, job: DownloadJob):
        """Worker executing stream resolution, yt-dlp download, subtitle extraction/embedding, and progress capture."""
        target_dir = get_downloads_dir()
        try:
            job.status = "extracting"
            job.broadcast()

            logger.info(f"[{job.job_id}] Resolving stream for {job.title} ({job.media_type})")
            s = int(job.season) if job.season else 1
            e = int(job.episode) if job.episode else 1

            # Proactive stream verification & fallback for unhosted placeholder IDs
            target_tmdb_id = job.tmdb_id
            if not check_stream_available(target_tmdb_id, job.media_type, s, e):
                candidates = search_media(job.title)
                for cand in candidates:
                    if cand["id"] != target_tmdb_id and cand["media_type"] == job.media_type:
                        if check_stream_available(cand["id"], cand["media_type"], s, e):
                            logger.info(
                                f"[{job.job_id}] Auto-routed unhosted TMDB {target_tmdb_id} to verified working stream TMDB {cand['id']} ('{cand['title']}' {cand['year']})"
                            )
                            target_tmdb_id = cand["id"]
                            job.tmdb_id = cand["id"]
                            break

            stream_info = extract_vixsrc_stream(
                tmdb_id=target_tmdb_id,
                media_type=job.media_type,
                season=s,
                episode=e,
            )

            master_playlist_url = stream_info["master_playlist_url"]
            headers = stream_info["http_headers"]
            stem = Path(job.filename).stem

            # Extract subtitles into dedicated cache ONLY (never cluttering the download folder)
            subtitles_list = stream_info.get("subtitles", [])
            if job.subtitles_enabled and subtitles_list and job.sub_lang.lower() != "none":
                try:
                    logger.info(f"[{job.job_id}] Fetching subtitles for {job.title} (lang: {job.sub_lang}) into cache")
                    subs = download_and_extract_subtitles(
                        subtitles_data=subtitles_list,
                        base_stem=stem,
                        preferred_lang=job.sub_lang,
                    )
                    job.subtitles_downloaded = subs
                    job.broadcast()
                except Exception as sub_e:
                    logger.warning(f"[{job.job_id}] Subtitle fetch error: {sub_e}")

            if job.status == "cancelled":
                cleanup_job_junk_files(job.filename, target_dir, clean_cache=True)
                return

            job.status = "downloading"
            job.broadcast()

            # Construct format selection string
            lang = job.audio_lang.lower()
            if job.quality == "1080p":
                fmt = f"bestvideo[height<=1080]+bestaudio[language={lang}]/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"
            elif job.quality == "720p":
                fmt = f"bestvideo[height<=720]+bestaudio[language={lang}]/bestvideo[height<=720]+bestaudio/best[height<=720]/best"
            elif job.quality == "480p":
                fmt = f"bestvideo[height<=480]+bestaudio[language={lang}]/bestvideo[height<=480]+bestaudio/best[height<=480]/best"
            else:
                fmt = f"bestvideo+bestaudio[language={lang}]/bestvideo+bestaudio/best"

            output_template = str(target_dir / f"{stem}.%(ext)s")

            cmd = [
                "yt-dlp",
                "-N", str(job.threads),
                "--buffer-size", "16M",
                "--http-chunk-size", "10M",
                "--hls-use-mpegts",
                "--retries", "10",
                "--fragment-retries", "10",
                "--add-header", f"Referer:{headers.get('Referer', 'https://vixsrc.to/')}",
                "--add-header", f"User-Agent:{headers.get('User-Agent', '')}",
                "-f", fmt,
                "--merge-output-format", "mp4",
                "--newline",
                "--progress-template", "%(progress._percent_str)s|%(progress._speed_str)s|%(progress._total_bytes_str)s|%(progress._eta_str)s",
                "-o", output_template,
            ]

            if job.preview_seconds:
                cmd.extend(["--download-sections", f"*00:00:00-00:00:{job.preview_seconds:02d}"])

            cmd.append(master_playlist_url)

            logger.info(f"[{job.job_id}] Starting yt-dlp command: {' '.join(cmd[:8])} ...")

            job.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
            )

            recent_lines = []
            for line in job.process.stdout:
                if job.status == "cancelled":
                    break

                line = line.strip()
                if not line:
                    continue

                recent_lines.append(line)
                if len(recent_lines) > 10:
                    recent_lines.pop(0)

                # Parse custom progress template: "percent|speed|total_bytes|eta"
                if "|" in line and "%" in line:
                    parts = line.split("|")
                    if len(parts) >= 4:
                        try:
                            pct_str = parts[0].replace("%", "").strip()
                            job.progress = float(pct_str)
                            job.speed = parts[1].strip() or job.speed
                            job.total_size_str = parts[2].strip() or job.total_size_str
                            job.eta = parts[3].strip() or job.eta
                            job.broadcast()
                        except Exception:
                            pass
                elif "[Merger]" in line or "Merging formats" in line:
                    job.status = "merging"
                    job.progress = 99.0
                    job.broadcast()
                elif "[download] 100%" in line:
                    job.progress = 100.0

            job.process.wait()

            if job.status == "cancelled":
                logger.info(f"[{job.job_id}] Download cancelled by user, cleaning fragments and cache.")
                time.sleep(0.3)
                cleanup_job_junk_files(job.filename, target_dir, clean_cache=True)
                return

            if job.process.returncode == 0:
                # Find the produced output file
                out_path = Path(job.filepath)
                if not out_path.exists():
                    # Check if saved with slight extension change
                    candidates = list(target_dir.glob(f"{stem}.*"))
                    for cand in candidates:
                        if cand.suffix.lower() in (".mp4", ".mkv", ".webm", ".ts"):
                            out_path = cand
                            job.filename = out_path.name
                            job.filepath = str(out_path)
                            break

                if out_path.exists():
                    # If subtitles were downloaded, embed into MP4 container from internal cache
                    if job.subtitles_enabled and job.subtitles_downloaded:
                        primary_srt = Path(job.subtitles_downloaded[0].get("srt_path", ""))
                        if not primary_srt.exists():
                            primary_srt = get_subtitles_cache_dir() / f"{stem}.srt"
                        if primary_srt.exists():
                            sub_lang = job.subtitles_downloaded[0].get("language", job.sub_lang)
                            sub_name = job.subtitles_downloaded[0].get("name", "English")
                            embed_subtitles_in_mp4(out_path, primary_srt, lang=sub_lang, title=sub_name)

                    # Ensure target_dir has NO stray temporary files or loose text files
                    cleanup_job_junk_files(job.filename, target_dir, clean_cache=False)

                    job.file_size = out_path.stat().st_size
                    job.status = "completed"
                    job.progress = 100.0
                    job.completed_at = time.time()
                    if job.file_size > 1024 * 1024 * 1024:
                        job.total_size_str = f"{job.file_size / (1024 * 1024 * 1024):.2f} GB"
                    elif job.file_size > 1024 * 1024:
                        job.total_size_str = f"{job.file_size / (1024 * 1024):.1f} MB"
                    else:
                        job.total_size_str = f"{job.file_size / 1024:.1f} KB"
                    logger.info(f"[{job.job_id}] Download completed: {job.filename} ({job.total_size_str})")
                else:
                    job.status = "failed"
                    job.error = "File was not found after process exited."
                    cleanup_job_junk_files(job.filename, target_dir, clean_cache=True)
            else:
                job.status = "failed"
                err_detail = " | ".join(recent_lines[-3:]) if recent_lines else ""
                job.error = f"yt-dlp error (code {job.process.returncode}): {err_detail}"
                logger.error(f"[{job.job_id}] Download failed: {job.error}")
                cleanup_job_junk_files(job.filename, target_dir, clean_cache=True)

        except Exception as e:
            logger.exception(f"[{job.job_id}] Unexpected error during download: {e}")
            job.status = "failed"
            job.error = str(e)
            cleanup_job_junk_files(job.filename, target_dir, clean_cache=True)
        finally:
            job.broadcast()
            self._schedule_next()


def scan_local_library() -> List[Dict[str, Any]]:
    """Scan the active local device downloads directory and return list of completed media files."""
    library = []
    target_dir = get_downloads_dir()
    cache_dir = get_subtitles_cache_dir()
    if not target_dir.exists():
        return []

    for item in sorted(target_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if item.is_file() and item.suffix.lower() in (".mp4", ".mkv", ".webm", ".avi", ".ts"):
            size = item.stat().st_size
            mtime = item.stat().st_mtime
            # Format size human readable
            if size > 1024 * 1024 * 1024:
                size_str = f"{size / (1024 * 1024 * 1024):.2f} GB"
            elif size > 1024 * 1024:
                size_str = f"{size / (1024 * 1024):.1f} MB"
            else:
                size_str = f"{size / 1024:.1f} KB"

            q_name = urllib.parse.quote(item.name)
            stem = item.stem
            vtt_cache = cache_dir / f"{stem}.vtt"
            has_subtitles = vtt_cache.exists() or (cache_dir / f"{stem}.srt").exists() or True
            subtitle_url = f"/api/subtitles/{urllib.parse.quote(stem)}.vtt" if vtt_cache.exists() else None

            library.append({
                "filename": item.name,
                "filepath": str(item),
                "size_bytes": size,
                "size_formatted": size_str,
                "modified_at": mtime,
                "modified_str": time.strftime("%b %d, %Y %I:%M %p", time.localtime(mtime)),
                "has_subtitles": has_subtitles,
                "subtitle_url": subtitle_url,
                "download_url": f"/api/files/{q_name}?download=1",
                "stream_url": f"/api/files/{q_name}",
            })
    return library


# Global Singleton Manager
manager = DownloadManager()
