"""
FastAPI Server & Route Handlers for HDTodayz Video Downloader
"""

import os
import re
import math
import asyncio
import subprocess
import shutil
import urllib.parse
from pathlib import Path
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, FileResponse, Response as RawResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.accelerator import accelerator_manager

from app.extractor import (
    resolve_media_info,
    fetch_tv_season_episodes,
    extract_vixsrc_stream,
    check_stream_available,
    search_media,
    get_trending_catalog,
)
from app.downloader import (
    manager,
    scan_local_library,
    get_downloads_dir,
    set_downloads_dir,
    get_subtitles_cache_dir,
    migrate_legacy_downloads,
    cleanup_all_junk_files,
    sanitize_filename,
    download_and_extract_subtitles,
)

app = FastAPI(
    title="HDTodayz Video Downloader",
    description="Automated video stream extractor and downloader for HDTodayz",
    version="1.0.0",
)


@app.on_event("startup")
async def on_startup():
    """Migrate legacy scratch downloads and sweep any orphaned fragment junk files."""
    migrate_legacy_downloads()
    cleanup_all_junk_files()

# Enable CORS for convenience
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
INDEX_HTML = STATIC_DIR / "index.html"

# Mount static assets
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class ResolveRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    tmdb_id: int
    title: str
    media_type: str = "movie"
    year: str = ""
    season: Optional[int] = None
    episode: Optional[int] = None
    episode_title: Optional[str] = None
    quality: str = "best"
    audio_lang: str = "eng"
    sub_lang: str = "eng"
    subtitles_enabled: bool = True
    poster_url: str = ""
    preview_seconds: Optional[int] = None
    threads: int = 16


class BatchEpisodeItem(BaseModel):
    episode: int
    episode_title: Optional[str] = None


class BatchDownloadRequest(BaseModel):
    tmdb_id: int
    title: str
    media_type: str = "tv"
    year: str = ""
    season: int = 1
    episodes: List[BatchEpisodeItem]
    quality: str = "best"
    audio_lang: str = "eng"
    sub_lang: str = "eng"
    subtitles_enabled: bool = True
    poster_url: str = ""
    threads: int = 16


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    """Serve the main web UI."""
    if not INDEX_HTML.exists():
        raise HTTPException(status_code=404, detail="Web UI not built yet.")
    return HTMLResponse(content=INDEX_HTML.read_text(encoding="utf-8"))


@app.post("/api/resolve")
async def api_resolve(req: ResolveRequest):
    """
    Resolve media details (title, poster, TMDB ID, seasons) from HDTodayz URL or search string.
    """
    if not req.url or not req.url.strip():
        raise HTTPException(status_code=400, detail="Please provide a valid HDTodayz URL or query.")

    try:
        media_info = resolve_media_info(req.url.strip())

        # If it's a movie, also fetch stream info ahead of time
        stream_info = None
        if media_info["media_type"] == "movie":
            try:
                stream_info = extract_vixsrc_stream(media_info["tmdb_id"], "movie")
            except Exception as e:
                # If stream preview fails, still allow resolving metadata
                stream_info = {"error": str(e), "video_qualities": [], "audio_tracks": []}

        return {
            "success": True,
            "media": media_info,
            "stream_info": stream_info,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/tv/{tmdb_id}/season/{season_number}")
async def api_tv_season_episodes(tmdb_id: int, season_number: int):
    """Fetch episode list for a specific season of a TV show."""
    episodes = fetch_tv_season_episodes(tmdb_id, season_number)
    return {"success": True, "episodes": episodes}


@app.get("/api/stream-info")
async def api_stream_info(
    tmdb_id: int = Query(...),
    media_type: str = Query("movie"),
    season: int = Query(1),
    episode: int = Query(1),
):
    """Extract stream qualities and audio tracks for given media."""
    try:
        info = extract_vixsrc_stream(tmdb_id, media_type, season, episode)
        return {"success": True, "stream_info": info}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/search")
async def api_search(q: str = Query("")):
    """Search for movies and TV shows."""
    results = search_media(q)
    return {"success": True, "results": results}


@app.get("/api/trending")
async def api_trending():
    """Fetch trending movies and TV series."""
    catalog = get_trending_catalog()
    return {"success": True, "catalog": catalog}


@app.get("/api/hls_accel/{sid}/key")
async def api_hls_accel_key(sid: str):
    sess = accelerator_manager.get_session(sid)
    if not sess or not sess.key_bytes:
        raise HTTPException(status_code=404, detail="Key not found")
    return RawResponse(content=sess.key_bytes, media_type="application/octet-stream")


@app.get("/api/hls_accel/{sid}/video.m3u8")
async def api_hls_accel_vid_m3u8(sid: str):
    sess = accelerator_manager.get_session(sid)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    return RawResponse(content=sess.vid_m3u8, media_type="application/vnd.apple.mpegurl")


@app.get("/api/hls_accel/{sid}/audio.m3u8")
async def api_hls_accel_aud_m3u8(sid: str):
    sess = accelerator_manager.get_session(sid)
    if not sess or not sess.aud_m3u8:
        raise HTTPException(status_code=404, detail="Audio stream not found")
    return RawResponse(content=sess.aud_m3u8, media_type="application/vnd.apple.mpegurl")


@app.get("/api/hls_accel/{sid}/v/{idx}.ts")
async def api_hls_accel_vid_seg(sid: str, idx: int):
    sess = accelerator_manager.get_session(sid)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    data = await sess.get_segment(idx, is_video=True)
    return RawResponse(content=data, media_type="video/mp2t")


@app.get("/api/hls_accel/{sid}/a/{idx}.ts")
async def api_hls_accel_aud_seg(sid: str, idx: int):
    sess = accelerator_manager.get_session(sid)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    data = await sess.get_segment(idx, is_video=False)
    return RawResponse(content=data, media_type="video/mp2t")


@app.get("/api/debug-upstream")
async def api_debug_upstream(tmdb_id: int = 1418, s: int = 1, e: int = 1, custom_url: Optional[str] = None):
    import urllib.request, urllib.error
    import aiohttp
    
    url = custom_url if custom_url else f"https://vixsrc.to/api/tv/{tmdb_id}/{s}/{e}"
    res = {"target": url}
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://www.vidking.net/" if "speedrace" in url or "vidking" in url else f"https://vixsrc.to/tv/{tmdb_id}/{s}/{e}",
        "Origin": "https://www.vidking.net" if "speedrace" in url or "vidking" in url else "https://vixsrc.to",
        "Accept": "*/*",
    }
    
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            res["urllib"] = {"status": r.status, "body": r.read().decode(errors="replace")[:400]}
    except urllib.error.HTTPError as ex:
        res["urllib"] = {"status": ex.code, "headers": dict(ex.headers), "body": ex.read().decode(errors="replace")[:400]}
    except Exception as ex:
        res["urllib"] = {"error": str(ex)}
        
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                text = await r.text()
                res["aiohttp"] = {"status": r.status, "headers": dict(r.headers), "body": text[:400]}
    except Exception as ex:
        res["aiohttp"] = {"error": str(ex)}

    try:
        from curl_cffi import requests as cffi_requests
        cffi_res = cffi_requests.get(url, headers=headers, impersonate="chrome124", timeout=10)
        res["curl_cffi"] = {"status": cffi_res.status_code, "headers": dict(cffi_res.headers), "body": cffi_res.text[:400]}
    except Exception as ex:
        res["curl_cffi"] = {"error": str(ex)}

    return res


@app.get("/api/stream/check")
async def api_check_stream(
    tmdb_id: int = Query(...),
    media_type: str = Query("movie"),
    season: int = Query(1),
    episode: int = Query(1),
):
    """Preflight check to verify if a stream is accessible from this server."""
    try:
        stream_info = extract_vixsrc_stream(tmdb_id, media_type, season, episode)
        return {
            "available": True,
            "server": stream_info.get("server", "VixSrc"),
            "qualities": [q.get("label") for q in stream_info.get("video_qualities", [])],
        }
    except Exception as ex:
        msg = str(ex)
        is_waf = "403" in msg or "Cloudflare" in msg or "datacenter" in msg.lower() or "Forbidden" in msg
        return {
            "available": False,
            "error": msg,
            "is_waf_blocked": is_waf,
            "message": (
                "Upstream streaming host blocked cloud datacenter IP (Cloudflare WAF HTTP 403). "
                "Cloud platforms (Render / AWS) are restricted by the third-party host. "
                "You can test high-speed downloading, in-flight MP4 remuxing, and multi-episode parallel downloads right now using the built-in 'Demo Showcase'!"
                if is_waf else msg
            ),
        }


@app.get("/api/download/stream")
async def api_direct_stream_download(
    request: Request,
    tmdb_id: int = Query(...),
    media_type: str = Query("movie"),
    title: str = Query("video"),
    year: str = Query(""),
    season: Optional[int] = Query(None),
    episode: Optional[int] = Query(None),
    episode_title: Optional[str] = Query(None),
    quality: str = Query("best"),
    audio_lang: str = Query("eng"),
    sub_lang: str = Query("eng"),
    subtitles_enabled: bool = Query(True),
    preview_seconds: Optional[int] = Query(None),
    threads: int = Query(16),
):
    """
    High-Speed Direct in-flight streaming endpoint.
    Concurrently prefetches HLS fragments into RAM with a 32-worker async pool,
    feeds them through a zero-latency loopback proxy to ffmpeg for fragmented MP4 remuxing,
    and pipes the stream directly into the client's browser with Content-Disposition: attachment.
    Delivers 10x+ download speeds with ZERO files stored on the host PC hard drive.
    """
    s = season if season else 1
    e = episode if episode else 1

    target_tmdb_id = tmdb_id
    if target_tmdb_id != 999999 and not check_stream_available(target_tmdb_id, media_type, s, e):
        candidates = search_media(title)
        for cand in candidates:
            if cand["id"] != target_tmdb_id and cand["media_type"] == media_type:
                if check_stream_available(cand["id"], cand["media_type"], s, e):
                    target_tmdb_id = cand["id"]
                    break

    try:
        stream_info = extract_vixsrc_stream(
            tmdb_id=target_tmdb_id,
            media_type=media_type,
            season=s,
            episode=e,
        )
    except Exception as ex:
        err_msg = str(ex)
        if "403" in err_msg or "Cloudflare" in err_msg or "Forbidden" in err_msg:
            return HTMLResponse(
                content="""
                <!DOCTYPE html>
                <html>
                <head>
                  <title>Stream Restricted on Cloud Datacenter</title>
                  <meta name="viewport" content="width=device-width, initial-scale=1.0">
                  <style>
                    body { background: #0b0f19; color: #e2e8f0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; padding: 1.5rem; box-sizing: border-box; }
                    .card { background: #1a2234; border: 1px solid #334155; border-radius: 14px; max-width: 540px; padding: 2.2rem; text-align: center; box-shadow: 0 20px 40px rgba(0,0,0,0.6); }
                    .icon { font-size: 3rem; margin-bottom: 0.5rem; }
                    h2 { color: #f87171; margin: 0 0 1rem; font-size: 1.4rem; }
                    p { color: #94a3b8; font-size: 0.95rem; line-height: 1.6; margin: 0 0 1.2rem; }
                    .tip-box { background: rgba(56, 189, 248, 0.1); border: 1px solid rgba(56, 189, 248, 0.3); border-radius: 8px; padding: 1rem; text-align: left; margin-bottom: 1.5rem; }
                    .tip-title { color: #38bdf8; font-weight: bold; font-size: 0.9rem; margin-bottom: 0.4rem; }
                    .tip-desc { color: #cbd5e1; font-size: 0.85rem; margin: 0; line-height: 1.5; }
                    .btn { display: inline-flex; align-items: center; gap: 8px; background: #2563eb; color: #fff; text-decoration: none; padding: 12px 24px; border-radius: 8px; font-weight: 600; font-size: 0.95rem; transition: background 0.2s; }
                    .btn:hover { background: #1d4ed8; }
                  </style>
                </head>
                <body>
                  <div class="card">
                    <div class="icon">🛡️</div>
                    <h2>Upstream Host Restricted on Cloud Datacenter</h2>
                    <p>The third-party streaming server blocks automated cloud datacenter IP addresses (Render / AWS) via Cloudflare WAF.</p>
                    <div class="tip-box">
                      <div class="tip-title">💡 How to test all features right now:</div>
                      <div class="tip-desc">You can test high-speed multi-worker downloading, simultaneous parallel episode downloads, and real-time MP4 remuxing using the built-in <strong>Demo Stream Showcase</strong>!</div>
                    </div>
                    <a href="/?resolve=demo" class="btn">🧪 Launch Working Demo Stream</a>
                  </div>
                </body>
                </html>
                """,
                status_code=403,
            )
        raise HTTPException(status_code=500, detail=f"Could not extract stream: {ex}")

    master_playlist_url = stream_info["master_playlist_url"]
    headers = stream_info["http_headers"]

    clean_title = sanitize_filename(title)
    prev_tag = f"_preview{preview_seconds}s" if preview_seconds else ""
    if media_type == "tv" and season and episode:
        ep_suffix = f"S{season:02d}E{episode:02d}"
        if episode_title:
            clean_ep = sanitize_filename(episode_title)
            final_filename = f"{clean_title}.{ep_suffix}.{clean_ep}{prev_tag}.mp4"
        else:
            final_filename = f"{clean_title}.{ep_suffix}{prev_tag}.mp4"
    else:
        yr_suffix = f"_{year}" if year else ""
        final_filename = f"{clean_title}{yr_suffix}{prev_tag}.mp4"

    # Concurrency: Turbo=32, High=24, Balanced=16
    concurrency = max(16, min(48, threads * 2))

    try:
        sid, has_audio, session = await accelerator_manager.create_session(
            master_url=master_playlist_url,
            http_headers=headers,
            quality=quality,
            audio_lang=audio_lang,
            preview_seconds=preview_seconds,
            concurrency=concurrency,
        )
    except Exception as ex:
        raise HTTPException(status_code=500, detail=f"Could not initialize stream accelerator: {ex}")

    sub_srt_path: Optional[Path] = None
    subtitles_list = stream_info.get("subtitles", [])
    if subtitles_enabled and subtitles_list and sub_lang.lower() != "none":
        try:
            stem = Path(final_filename).stem
            subs = download_and_extract_subtitles(
                subtitles_data=subtitles_list,
                base_stem=stem,
                preferred_lang=sub_lang,
            )
            if subs and Path(subs[0]["srt_path"]).exists():
                sub_srt_path = Path(subs[0]["srt_path"])
        except Exception:
            sub_srt_path = None

    port = os.environ.get("PORT", "7860")
    loopback = f"http://127.0.0.1:{port}"

    cmd = [
        "ffmpeg", "-y",
        "-i", f"{loopback}/api/hls_accel/{sid}/video.m3u8",
    ]
    if has_audio:
        cmd.extend([
            "-i", f"{loopback}/api/hls_accel/{sid}/audio.m3u8",
        ])

    if sub_srt_path and sub_srt_path.exists():
        cmd.extend([
            "-i", str(sub_srt_path),
            "-c:v", "copy",
            "-c:a", "copy",
            "-c:s", "mov_text",
            "-bsf:a", "aac_adtstoasc",
            "-flush_packets", "1",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4",
            "pipe:1",
        ])
    else:
        cmd.extend([
            "-c:v", "copy",
            "-c:a", "copy",
            "-bsf:a", "aac_adtstoasc",
            "-flush_packets", "1",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4",
            "pipe:1",
        ])

    async def stream_generator():
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        loop = asyncio.get_event_loop()
        try:
            while True:
                if await request.is_disconnected():
                    break
                chunk = await loop.run_in_executor(None, proc.stdout.read, 131072)
                if not chunk:
                    break
                yield chunk
        finally:
            if proc and proc.poll() is None:
                try:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=2)
                    else:
                        proc.kill()
                except Exception:
                    pass
            await accelerator_manager.remove_session(sid)
            if sub_srt_path and sub_srt_path.exists():
                try:
                    sub_srt_path.unlink(missing_ok=True)
                except Exception:
                    pass

    encoded_filename = urllib.parse.quote(final_filename)
    return StreamingResponse(
        stream_generator(),
        media_type="video/mp4",
        headers={
            "Content-Disposition": f'attachment; filename="{final_filename}"; filename*=UTF-8\'\'{encoded_filename}',
            "Content-Type": "video/mp4",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/download")
async def api_start_download(req: DownloadRequest):
    """Start a background download job."""
    job = manager.create_job(
        tmdb_id=req.tmdb_id,
        title=req.title,
        media_type=req.media_type,
        year=req.year,
        season=req.season,
        episode=req.episode,
        episode_title=req.episode_title,
        quality=req.quality,
        audio_lang=req.audio_lang,
        sub_lang=req.sub_lang,
        subtitles_enabled=req.subtitles_enabled,
        poster_url=req.poster_url,
        preview_seconds=req.preview_seconds,
        threads=req.threads,
    )
    return {"success": True, "job": job.to_dict()}


@app.post("/api/download/batch")
async def api_start_batch_download(req: BatchDownloadRequest):
    """Start batch download jobs for multiple TV episodes."""
    if not req.episodes:
        raise HTTPException(status_code=400, detail="No episodes provided for batch download.")

    ep_dicts = [
        {"episode": ep.episode, "episode_title": ep.episode_title}
        for ep in req.episodes
    ]
    jobs = manager.create_batch_jobs(
        tmdb_id=req.tmdb_id,
        title=req.title,
        media_type=req.media_type,
        year=req.year,
        season=req.season,
        episodes=ep_dicts,
        quality=req.quality,
        audio_lang=req.audio_lang,
        sub_lang=req.sub_lang,
        subtitles_enabled=req.subtitles_enabled,
        poster_url=req.poster_url,
        threads=req.threads,
    )
    return {
        "success": True,
        "count": len(jobs),
        "jobs": [j.to_dict() for j in jobs],
    }


@app.post("/api/cleanup-junk")
async def api_cleanup_junk():
    """Immediately remove all orphaned download fragments (.part, .ytdl, etc.) from disk."""
    count = cleanup_all_junk_files()
    return {
        "success": True,
        "count": count,
        "message": f"Cleaned {count} temporary files.",
    }


@app.get("/api/jobs")
async def api_list_jobs():
    """List all current and past download jobs."""
    return {"success": True, "jobs": manager.list_jobs()}


@app.get("/api/jobs/{job_id}")
async def api_get_job(job_id: str):
    """Get status of a specific job."""
    job = manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"success": True, "job": job.to_dict()}


@app.post("/api/jobs/{job_id}/cancel")
async def api_cancel_job(job_id: str):
    """Cancel an active download job."""
    success = manager.cancel_job(job_id)
    if not success:
        raise HTTPException(status_code=404, detail="Job not found or already finished")
    return {"success": True, "message": "Job cancelled"}


@app.delete("/api/jobs/{job_id}")
async def api_delete_job(job_id: str, delete_file: bool = False):
    """Delete a job and optionally its file."""
    success = manager.delete_job(job_id, delete_file=delete_file)
    return {"success": success}


@app.get("/api/jobs/{job_id}/stream")
async def api_job_sse_stream(job_id: str, request: Request):
    """Server-Sent Events endpoint streaming live download progress."""
    job = manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_generator():
        queue = asyncio.Queue()
        job.listeners.add(queue)

        # Emit initial state immediately
        initial_data = job.to_dict()
        yield f"data: {JSONResponse(initial_data).body.decode()}\n\n"

        try:
            while True:
                if await request.is_disconnected():
                    break

                try:
                    # Wait up to 1 second for state update or emit heartbeat ping
                    data = await asyncio.wait_for(queue.get(), timeout=1.0)
                    yield f"data: {JSONResponse(data).body.decode()}\n\n"
                    if data["status"] in ("completed", "failed", "cancelled"):
                        break
                except asyncio.TimeoutError:
                    # Keep-alive heartbeat ping to prevent proxy/browser timeout
                    yield ": ping\n\n"
        finally:
            job.listeners.discard(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform, no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


class StorageConfigUpdate(BaseModel):
    download_dir: str


@app.get("/api/storage/config")
async def api_get_storage_config():
    """Retrieve the current download directory and disk information."""
    down_dir = get_downloads_dir()
    total_bytes = 0
    free_bytes = 0
    try:
        usage = shutil.disk_usage(str(down_dir))
        free_bytes = usage.free
        total_bytes = usage.total
    except Exception:
        pass

    file_count = 0
    if down_dir.exists():
        file_count = sum(
            1 for f in down_dir.iterdir()
            if f.is_file() and f.suffix.lower() in (".mp4", ".mkv", ".webm", ".avi", ".ts")
        )

    return {
        "success": True,
        "download_dir": str(down_dir),
        "exists": down_dir.exists(),
        "free_gb": round(free_bytes / (1024 ** 3), 1),
        "total_gb": round(total_bytes / (1024 ** 3), 1),
        "file_count": file_count,
    }


@app.post("/api/storage/config")
async def api_set_storage_config(req: StorageConfigUpdate):
    """Set and persist a new download directory path."""
    new_path = req.download_dir.strip()
    if not new_path:
        raise HTTPException(status_code=400, detail="Path cannot be empty")
    try:
        p = set_downloads_dir(new_path)
        return {
            "success": True,
            "download_dir": str(p),
            "message": f"Storage directory set to: {p}",
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid path: {e}")


@app.get("/api/library")
async def api_get_library():
    """List completed files in the downloads directory."""
    files = scan_local_library()
    return {
        "success": True,
        "download_dir": str(get_downloads_dir()),
        "library": files,
    }


@app.delete("/api/library/{filename}")
async def api_delete_library_file(filename: str):
    """Delete a file and any associated subtitles from the downloads directory and cache."""
    clean_name = Path(filename).name
    down_dir = get_downloads_dir()
    target = down_dir / clean_name
    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")
    try:
        target.unlink()
        stem = target.stem
        for sf in down_dir.glob(f"{stem}*.srt"):
            try: sf.unlink()
            except Exception: pass
        for vf in down_dir.glob(f"{stem}*.vtt"):
            try: vf.unlink()
            except Exception: pass
        # Also clean up cached subtitles
        cache_dir = get_subtitles_cache_dir()
        if cache_dir.exists():
            for cf in cache_dir.glob(f"{stem}*"):
                try: cf.unlink()
                except Exception: pass
        return {"success": True, "message": f"Deleted {clean_name} and subtitles"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete file: {e}")


@app.get("/api/subtitles/{vtt_name}")
async def api_serve_cached_subtitle(vtt_name: str):
    """Serve cached WebVTT subtitle track for HTML5 video player."""
    clean_name = Path(vtt_name).name
    cache_dir = get_subtitles_cache_dir()
    sub_path = cache_dir / clean_name
    if not sub_path.exists() or not sub_path.is_file():
        raise HTTPException(status_code=404, detail="Subtitle file not found in cache")
    return FileResponse(
        path=str(sub_path),
        media_type="text/vtt; charset=utf-8",
        filename=clean_name,
        content_disposition_type="inline",
    )


@app.get("/api/files/{filename}")
@app.head("/api/files/{filename}")
async def api_serve_file(filename: str, request: Request, download: Optional[int] = None):
    """
    Serve video or subtitle file with native HTTP Range partial-content support for HTML5 video player seeking and direct download.
    """
    clean_name = Path(filename).name
    file_path = get_downloads_dir() / clean_name

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    content_type = "video/mp4"
    if clean_name.endswith(".mkv"):
        content_type = "video/x-matroska"
    elif clean_name.endswith(".webm"):
        content_type = "video/webm"
    elif clean_name.endswith(".vtt"):
        content_type = "text/vtt; charset=utf-8"
    elif clean_name.endswith(".srt"):
        content_type = "application/x-subrip; charset=utf-8"

    disp_type = "attachment" if download else "inline"
    return FileResponse(
        path=str(file_path),
        media_type=content_type,
        filename=clean_name,
        content_disposition_type=disp_type,
    )


@app.post("/api/play-local/{filename}")
async def api_play_local(filename: str):
    """Open file in local system default video player (e.g. VLC, Windows Media Player)."""
    clean_name = Path(filename).name
    file_path = (get_downloads_dir() / clean_name).resolve()
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    try:
        if os.name == "nt":
            os.startfile(str(file_path))
        else:
            subprocess.Popen(["xdg-open", str(file_path)])
        return {"success": True, "message": f"Opened {clean_name} in local media player"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to launch player: {e}")


@app.post("/api/open-folder")
async def api_open_folder(request: Request):
    """Open downloads folder in Windows File Explorer, optionally selecting the file."""
    try:
        body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    except Exception:
        body = {}
    filename = body.get("filename")
    down_dir = get_downloads_dir().resolve()
    try:
        if filename:
            clean_name = Path(filename).name
            file_path = (down_dir / clean_name).resolve()
            if file_path.exists() and os.name == "nt":
                subprocess.Popen(["explorer.exe", f"/select,{file_path}"])
                return {"success": True, "message": f"Opened folder with {clean_name} selected"}
        if os.name == "nt":
            os.startfile(str(down_dir))
        else:
            subprocess.Popen(["xdg-open", str(down_dir)])
        return {"success": True, "message": f"Opened folder: {down_dir}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to open folder: {e}")

