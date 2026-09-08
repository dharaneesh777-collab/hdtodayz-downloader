"""
HDTodayz Stream & Metadata Extractor
Handles URL resolution, TMDB metadata enrichment, and multi-server HLS stream extraction.
"""

import re
import json
import time
import base64
import threading
import logging
import urllib.request
import urllib.parse
import urllib.error
from typing import Dict, Any, List, Optional

logger = logging.getLogger("hdtoday.extractor")
logging.basicConfig(level=logging.INFO)

TMDB_API_KEY = "7b9720202f99648b367a5474d01c5d0e"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def _http_get(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 15) -> str:
    """Helper to perform HTTP GET requests with custom headers."""
    req_headers = dict(DEFAULT_HEADERS)
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _http_get_json(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 15) -> Any:
    """Helper to fetch and parse JSON."""
    raw = _http_get(url, headers=headers, timeout=timeout)
    return json.loads(raw)


DEMO_MEDIA_ID = 999999
DEMO_MEDIA_DATA = {
    "tmdb_id": DEMO_MEDIA_ID,
    "media_type": "tv",
    "title": "Big Buck Bunny (HLS Test Showcase)",
    "year": "2024",
    "release_date": "2024-01-01",
    "overview": "Official high-speed HLS demo showcase stream (Blender Foundation). 100% reliable for testing multi-worker prefetching, simultaneous multi-episode downloads, 1080p/720p/480p quality selection, and in-flight MP4 remuxing on any cloud provider without datacenter blocks.",
    "poster_url": "https://upload.wikimedia.org/wikipedia/commons/thumb/c/c5/Big_buck_bunny_poster_big.jpg/600px-Big_buck_bunny_poster_big.jpg",
    "backdrop_url": "https://peach.blender.org/wp-content/uploads/bbb-splash.png",
    "genres": ["Animation", "Action", "Demo Showcase"],
    "rating": 9.9,
    "runtime_minutes": 10,
    "stream_available": True,
    "hdtoday_url": "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8",
    "seasons": [
        {
            "season_number": 1,
            "name": "Season 1 (Demo Episodes)",
            "episode_count": 3,
            "episodes": [],
        }
    ],
    "number_of_seasons": 1,
    "number_of_episodes": 3,
}

DEMO_EPISODES = [
    {
        "episode_number": 1,
        "name": "The Forest Awakening (High Speed Test)",
        "overview": "First segment showcase testing turbo multi-worker fragment streaming and in-flight MP4 remuxing.",
        "air_date": "2024-01-01",
        "still_url": "https://peach.blender.org/wp-content/uploads/bbb-splash.png",
        "rating": 9.8,
    },
    {
        "episode_number": 2,
        "name": "The Rabbit's Revenge (Parallel Download Test)",
        "overview": "Second segment showcase testing simultaneous parallel episode downloading in Chrome.",
        "air_date": "2024-01-02",
        "still_url": "https://peach.blender.org/wp-content/uploads/bbb-splash.png",
        "rating": 9.7,
    },
    {
        "episode_number": 3,
        "name": "Flight of the Butterfly (Full Showcase)",
        "overview": "Third segment showcase testing 1080p HD bitstream parsing and real-time audio multiplexing.",
        "air_date": "2024-01-03",
        "still_url": "https://peach.blender.org/wp-content/uploads/bbb-splash.png",
        "rating": 9.9,
    },
]


def check_stream_available(
    tmdb_id: int,
    media_type: str = "movie",
    season: int = 1,
    episode: int = 1,
) -> bool:
    """
    Quickly probe streaming servers to verify if media stream is active.
    Returns True if an active stream is discovered across any server.
    """
    if tmdb_id == DEMO_MEDIA_ID:
        return True

    # 1. Quick check Server 5 (VidKing / SpeedRaceLight)
    try:
        seed = get_speedrace_seed(tmdb_id)
        if seed:
            return True
    except Exception:
        pass

    # 2. Probe Server 1 (VixSrc)
    try:
        if media_type == "tv":
            api_url = f"https://vixsrc.to/api/tv/{tmdb_id}/{season}/{episode}"
            referer = f"https://vixsrc.to/tv/{tmdb_id}/{season}/{episode}"
        else:
            api_url = f"https://vixsrc.to/api/movie/{tmdb_id}"
            referer = f"https://vixsrc.to/movie/{tmdb_id}"

        headers = {
            "Referer": referer,
            "User-Agent": DEFAULT_HEADERS["User-Agent"],
        }
        api_resp = _http_get_json(api_url, headers=headers, timeout=5)
        return bool(api_resp and api_resp.get("src"))
    except Exception:
        pass

    return False


def parse_url_target(input_str: str) -> Dict[str, Any]:
    """
    Parse a user-supplied string, which may be:
    - HDTodayz URL:
      - https://hdtodayz.org/movie/mayday
      - https://hdtodayz.org/watch/movie/1137844/mayday
      - https://hdtodayz.org/tv/silo
      - https://hdtodayz.org/watch/tv/125988/silo
      - https://hdtodayz.org/home
    - Direct TMDB ID: e.g. 1137844
    - Search query: e.g. "Silo" or "Mayday"
    """
    input_str = input_str.strip()

    # Demo or Test Trigger
    if input_str.lower() in ("demo", "test", "sample", "demo showcase", "999999", "big buck bunny"):
        return {"type": "demo", "tmdb_id": DEMO_MEDIA_ID, "media_type": "tv"}

    # Direct TMDB ID
    if input_str.isdigit():
        return {"type": "tmdb_id", "tmdb_id": int(input_str), "media_type": "movie"}

    # HDTodayz Watch URL with ID: /watch/(movie|tv)/(\d+)(?:/([^/?#]+))?
    watch_match = re.search(r"/(?:watch/)?(movie|tv)/(\d+)(?:/([^/?#]+))?", input_str)
    if watch_match:
        m_type = watch_match.group(1)
        tmdb_id = int(watch_match.group(2))
        slug = watch_match.group(3) or ""
        return {
            "type": "hdtoday_watch",
            "media_type": m_type,
            "tmdb_id": tmdb_id,
            "slug": slug,
            "url": input_str if input_str.startswith("http") else f"https://hdtodayz.org/watch/{m_type}/{tmdb_id}/{slug}",
        }

    # HDTodayz Slug URL: /(movie|tv)/([^/?#]+)
    slug_match = re.search(r"hdtodayz\.org/(movie|tv)/([^/?#]+)", input_str)
    if slug_match:
        m_type = slug_match.group(1)
        slug = slug_match.group(2)
        return {
            "type": "hdtoday_slug",
            "media_type": m_type,
            "slug": slug,
            "url": input_str if input_str.startswith("http") else f"https://hdtodayz.org/{m_type}/{slug}",
        }

    # Homepage or Browse URL
    if "hdtodayz.org" in input_str:
        return {"type": "hdtoday_browse", "url": input_str}

    # Otherwise treat as search query
    return {"type": "search_query", "query": input_str}


def resolve_media_info(target_input: str) -> Dict[str, Any]:
    """
    Resolve complete metadata (TMDB ID, title, overview, poster, seasons/episodes)
    from an HDTodayz URL or search string.
    """
    parsed = parse_url_target(target_input)
    tmdb_id = parsed.get("tmdb_id")
    media_type = parsed.get("media_type", "movie")
    slug = parsed.get("slug")

    if tmdb_id == DEMO_MEDIA_ID or parsed.get("type") == "demo":
        return dict(DEMO_MEDIA_DATA)

    # If we have an HDTodayz slug URL without TMDB ID, scrape the page to get TMDB ID
    if parsed["type"] == "hdtoday_slug":
        url = parsed["url"]
        try:
            html = _http_get(url)
            # Find watch link or TMDB ID
            watch_link_match = re.search(r'href="(/watch/(?:movie|tv)/(\d+)/[^"]+)"', html)
            if watch_link_match:
                tmdb_id = int(watch_link_match.group(2))
            else:
                # Try finding from schema.org JSON-LD
                id_matches = re.findall(r'"significantLinks":\s*\["/watch/(?:movie|tv)/(\d+)', html)
                if id_matches:
                    tmdb_id = int(id_matches[0])
        except Exception as e:
            logger.warning(f"Failed to fetch HDTodayz page {url}: {e}")

    # If still no TMDB ID, search TMDB for the slug or query
    if not tmdb_id:
        query = slug.replace("-", " ") if slug else parsed.get("query", target_input)
        search_res = search_media(query)
        if search_res:
            top_match = search_res[0]
            tmdb_id = top_match["id"]
            media_type = top_match["media_type"]
        else:
            raise ValueError(f"Could not identify movie or TV show from input: {target_input}")

    # Now enrich metadata via TMDB API
    tmdb_endpoint = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}?api_key={TMDB_API_KEY}&append_to_response=external_ids,credits,videos"
    data = _http_get_json(tmdb_endpoint)

    title = data.get("title") or data.get("name") or "Unknown Title"

    # Verify whether VixSrc has an active stream for this TMDB ID
    stream_available = check_stream_available(tmdb_id, media_type, season=1, episode=1)
    if not stream_available:
        logger.warning(
            f"TMDB ID {tmdb_id} ('{title}') has no active stream on VixSrc. Searching for verified working releases..."
        )
        candidates = search_media(title)
        for cand in candidates:
            c_id = cand["id"]
            c_type = cand["media_type"]
            if c_id != tmdb_id and c_type == media_type:
                if check_stream_available(c_id, c_type, season=1, episode=1):
                    logger.info(
                        f"Auto-routed unhosted release {tmdb_id} to verified working release: "
                        f"TMDB ID {c_id} ('{cand['title']}' {cand['year']})"
                    )
                    tmdb_id = c_id
                    # Re-fetch metadata with verified working ID
                    tmdb_endpoint = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}?api_key={TMDB_API_KEY}&append_to_response=external_ids,credits,videos"
                    data = _http_get_json(tmdb_endpoint)
                    title = data.get("title") or data.get("name") or title
                    stream_available = True
                    break

    release_date = data.get("release_date") or data.get("first_air_date") or ""
    year = release_date[:4] if release_date else ""
    overview = data.get("overview") or ""
    poster_path = data.get("poster_path")
    backdrop_path = data.get("backdrop_path")
    poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else ""
    backdrop_url = f"https://image.tmdb.org/t/p/w1280{backdrop_path}" if backdrop_path else ""
    genres = [g["name"] for g in data.get("genres", [])]
    vote_average = round(data.get("vote_average", 0), 1)
    runtime = data.get("runtime") or (data.get("episode_run_time", [0])[0] if data.get("episode_run_time") else 0)

    result: Dict[str, Any] = {
        "tmdb_id": tmdb_id,
        "media_type": media_type,
        "title": title,
        "year": year,
        "release_date": release_date,
        "overview": overview,
        "poster_url": poster_url,
        "backdrop_url": backdrop_url,
        "genres": genres,
        "rating": vote_average,
        "runtime_minutes": runtime,
        "stream_available": stream_available,
        "hdtoday_url": (
            f"https://hdtodayz.org/watch/{media_type}/{tmdb_id}"
        ),
    }

    # If TV show, fetch season and episode details
    if media_type == "tv":
        seasons_data = []
        raw_seasons = data.get("seasons", [])
        for s in raw_seasons:
            s_num = s.get("season_number")
            if s_num is None or s_num == 0:
                continue  # Skip specials by default
            ep_count = s.get("episode_count", 0)
            seasons_data.append({
                "season_number": s_num,
                "name": s.get("name") or f"Season {s_num}",
                "episode_count": ep_count,
                "episodes": [],  # lazy or populated on request
            })
        result["seasons"] = seasons_data
        result["number_of_seasons"] = data.get("number_of_seasons", len(seasons_data))
        result["number_of_episodes"] = data.get("number_of_episodes", 0)

    return result


def fetch_tv_season_episodes(tmdb_id: int, season_number: int) -> List[Dict[str, Any]]:
    """Fetch episode list for a specific season of a TV show."""
    if tmdb_id == DEMO_MEDIA_ID:
        return list(DEMO_EPISODES)

    endpoint = f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_number}?api_key={TMDB_API_KEY}"
    try:
        data = _http_get_json(endpoint)
        episodes = []
        for ep in data.get("episodes", []):
            episodes.append({
                "episode_number": ep.get("episode_number"),
                "name": ep.get("name") or f"Episode {ep.get('episode_number')}",
                "overview": ep.get("overview") or "",
                "air_date": ep.get("air_date") or "",
                "still_url": f"https://image.tmdb.org/t/p/w300{ep['still_path']}" if ep.get("still_path") else "",
                "rating": round(ep.get("vote_average", 0), 1),
            })
        return episodes
    except Exception as e:
        logger.error(f"Error fetching season {season_number} for TV {tmdb_id}: {e}")
        return []


def extract_vixsrc_stream(
    tmdb_id: int,
    media_type: str = "movie",
    season: int = 1,
    episode: int = 1,
) -> Dict[str, Any]:
    """
    Extract HLS stream information from VixSrc (HDTodayz primary server).
    Returns playlist URL, available video formats, audio tracks, and headers.
    """
    if tmdb_id == DEMO_MEDIA_ID:
        return {
            "server": "Demo Stream Server (Mux Open HLS)",
            "master_playlist_url": "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8",
            "video_qualities": [
                {"resolution": "1920x1080", "label": "1080p", "bandwidth": 6221600},
                {"resolution": "1280x720", "label": "720p", "bandwidth": 2149280},
                {"resolution": "848x480", "label": "480p", "bandwidth": 836280},
            ],
            "audio_tracks": [{"name": "English", "language": "eng"}],
            "subtitles": [],
            "http_headers": {
                "User-Agent": DEFAULT_HEADERS["User-Agent"],
            },
        }

    if media_type == "tv":
        api_url = f"https://vixsrc.to/api/tv/{tmdb_id}/{season}/{episode}"
        referer = f"https://vixsrc.to/tv/{tmdb_id}/{season}/{episode}"
    else:
        api_url = f"https://vixsrc.to/api/movie/{tmdb_id}"
        referer = f"https://vixsrc.to/movie/{tmdb_id}"

    headers = {
        "Referer": referer,
        "User-Agent": DEFAULT_HEADERS["User-Agent"],
    }

    # Step 1: Call VixSrc API
    try:
        api_resp = _http_get_json(api_url, headers=headers)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ValueError(
                f"Stream not hosted on server (HTTP 404). "
                f"This {'episode' if media_type == 'tv' else 'movie'} is not currently available on the HDTodayz streaming server."
            )
        elif e.code == 403:
            raise ValueError(
                f"Upstream provider blocked datacenter IP (Cloudflare WAF HTTP 403). "
                f"Cloud platforms (Render/AWS) are restricted by the third-party host. "
                f"Please use the built-in 'Test Demo Stream' to test all downloader features, "
                f"or run the server locally on a residential ISP network."
            )
        raise ValueError(f"Streaming server error: HTTP {e.code} {e.reason}")
    except Exception as e:
        raise ValueError(f"Failed to connect to streaming server: {e}")

    embed_path = api_resp.get("src")
    if not embed_path:
        raise ValueError(f"VixSrc API did not return stream source: {api_resp}")

    embed_url = "https://vixsrc.to" + embed_path

    # Step 2: Fetch embed page
    try:
        embed_html = _http_get(embed_url, headers=headers)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ValueError("Stream embed page not found on server (HTTP 404).")
        raise ValueError(f"Stream embed server error: HTTP {e.code} {e.reason}")

    # Step 3: Extract master playlist info
    token_m = re.search(r"'token':\s*'([^']+)'", embed_html)
    expires_m = re.search(r"'expires':\s*'([^']+)'", embed_html)
    url_m = re.search(r"url:\s*'([^']+)'", embed_html)

    if not (token_m and expires_m and url_m):
        # Fallback regex without quotes
        token_m = re.search(r"token:\s*['\"]([^'\"]+)['\"]", embed_html)
        expires_m = re.search(r"expires:\s*['\"]([^'\"]+)['\"]", embed_html)
        url_m = re.search(r"url:\s*['\"]([^'\"]+)['\"]", embed_html)

    if not (token_m and expires_m and url_m):
        raise ValueError("Failed to extract master playlist token from embed page.")

    token = token_m.group(1)
    expires = expires_m.group(1)
    base_playlist_url = url_m.group(1)

    sep = "&" if "?" in base_playlist_url else "?"
    master_playlist_url = f"{base_playlist_url}{sep}token={token}&expires={expires}&h=1"

    # Step 4: Fetch Master Playlist to discover available streams
    master_headers = {
        "Referer": "https://vixsrc.to/",
        "User-Agent": DEFAULT_HEADERS["User-Agent"],
    }
    try:
        playlist_text = _http_get(master_playlist_url, headers=master_headers)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ValueError("Master HLS playlist not found on server (HTTP 404).")
        raise ValueError(f"Playlist server error: HTTP {e.code} {e.reason}")

    # Parse qualities, audio, and subtitles from M3U8
    video_qualities = []
    audio_tracks = []
    subtitles = []

    for line in playlist_text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            # e.g. BANDWIDTH=1800000,RESOLUTION=1280x720,AUDIO="audio",SUBTITLES="subs"
            res_m = re.search(r"RESOLUTION=(\d+x\d+)", line)
            bw_m = re.search(r"BANDWIDTH=(\d+)", line)
            resolution = res_m.group(1) if res_m else "Unknown"
            height = resolution.split("x")[1] if "x" in resolution else "720"
            video_qualities.append({
                "resolution": resolution,
                "label": f"{height}p",
                "bandwidth": int(bw_m.group(1)) if bw_m else 0,
            })
        elif line.startswith("#EXT-X-MEDIA:TYPE=AUDIO"):
            # e.g. NAME="English",LANGUAGE="eng",URI="..."
            name_m = re.search(r'NAME="([^"]+)"', line)
            lang_m = re.search(r'LANGUAGE="([^"]+)"', line)
            name = name_m.group(1) if name_m else "Default"
            lang = lang_m.group(1) if lang_m else "und"
            audio_tracks.append({"name": name, "language": lang})
        elif line.startswith("#EXT-X-MEDIA:TYPE=SUBTITLES"):
            name_m = re.search(r'NAME="([^"]+)"', line)
            lang_m = re.search(r'LANGUAGE="([^"]+)"', line)
            uri_m = re.search(r'URI="([^"]+)"', line)
            def_m = re.search(r'DEFAULT=(YES|NO)', line)
            forced_m = re.search(r'FORCED=(YES|NO)', line)
            name = name_m.group(1) if name_m else "Sub"
            lang = lang_m.group(1) if lang_m else "und"
            uri = uri_m.group(1) if uri_m else ""
            if uri and not uri.startswith("http"):
                uri = urllib.parse.urljoin(master_playlist_url, uri)
            subtitles.append({
                "name": name,
                "language": lang,
                "uri": uri,
                "default": def_m.group(1) == "YES" if def_m else False,
                "forced": forced_m.group(1) == "YES" if forced_m else False,
            })

    # Sort video qualities highest first
    video_qualities.sort(key=lambda q: q["bandwidth"], reverse=True)

    return {
        "server": "Server 1 (VixSrc)",
        "master_playlist_url": master_playlist_url,
        "video_qualities": video_qualities,
        "audio_tracks": audio_tracks,
        "subtitles": subtitles,
        "http_headers": {
            "Referer": "https://vixsrc.to/",
            "User-Agent": DEFAULT_HEADERS["User-Agent"],
        },
    }


# SpeedRace / VidKing 32-bit cipher constants
_HL = [
    1116352408, 1899447441, 3049323471, 3921009573, 961987163, 1508970993,
    2453635748, 2870763221, 3624381080, 310598401, 607225278, 1426881987,
    1925078388, 2162078206, 2614888103, 3248222580
]
_F_CONST = [1732584193, 4023233417, 2562383102, 271733878]
_JS = 61
_SF = 8
_MS = 2654435769
_YS = [109, 118, 109, 49]  # "mvm1"

_SEED_CACHE: Dict[int, tuple] = {}
_SEED_LOCK = threading.Lock()


def _u32(val: int) -> int:
    return val & 0xFFFFFFFF


def _imul(a: int, b: int) -> int:
    a = a & 0xFFFFFFFF
    b = b & 0xFFFFFFFF
    if a >= 0x80000000:
        a -= 0x100000000
    if b >= 0x80000000:
        b -= 0x100000000
    return (a * b) & 0xFFFFFFFF


def _ci(l: int) -> int:
    l = _u32(l)
    l ^= (l >> 16)
    l = _imul(l, 2246822507)
    l ^= (l >> 13)
    l = _imul(l, 3266489909)
    l ^= (l >> 16)
    return _u32(l)


def _ps(l: int, o: int) -> int:
    l = _u32(l)
    o = o & 31
    if o == 0:
        return l
    return _u32((l << o) | (l >> (32 - o)))


def _Af(l_str: str) -> int:
    o = _u32(_F_CONST[0])
    for e in range(len(l_str)):
        o = _ps(_u32(o ^ _imul(ord(l_str[e]), _HL[e & 15])), 5)
    return _ci(o)


def _wf(l_str: str) -> dict:
    o = {i: i for i in range(256)}
    e = 0
    n = len(l_str)
    for i in range(256):
        e = (e + o[i] + ord(l_str[i % n])) & 255
        o[i], o[e] = o[e], o[i]
    return o


def _vf(l_str: str) -> int:
    o = 2166136261
    for ch in l_str:
        o = _imul(o ^ ord(ch), 16777619)
    return _ci(o)


def _Nf(l: int, o: int, e: int) -> int:
    return _u32((l ^ o) | (l & o & e))


def _bf(l: int) -> bool:
    return ((l * (l + 1)) & 1) == 0


def _If(l: int) -> bool:
    return ((l * (l + 1)) & 1) == 1


def _Rf(l_str: str, o: int) -> dict:
    if _If(len(l_str)):
        return {"S": _wf(l_str), "acc": _Af(l_str)}
    e = {}
    i = _ci(_vf(l_str) ^ _ci(_u32(o) ^ _MS))
    for r in range(_SF):
        if _bf(r):
            n = i % _JS
            i = _ps(_u32(i + _MS), 7 + (r & 7))
            e[n] = _u32(i ^ _ci(i))
            i = _ci(_u32(i + n))
        else:
            e[r] = _HL[r & 15]
    return {"S": e, "acc": _u32(_ci(i ^ 2779096485))}


def _Cf(l_obj: dict, o: int) -> int:
    e = l_obj["S"]
    i = l_obj["acc"]
    r = i % _JS
    n = -1 if (r in e) else 0
    u = _u32(e.get(r, 0))
    d = _imul(_MS, o + 1)
    g = _Nf(i, _u32(u ^ d), n)
    g = _u32(_ps(_u32(g + i), r & 31) ^ _ps(i, (r * 7) & 31))
    i = _ci(_u32(g + _MS))
    e[r] = i
    l_obj["acc"] = i
    return i


def _xf(l_str: str, o: int, e_len: int) -> bytearray:
    i = _Rf(l_str, o)
    r = bytearray(e_len)
    u = 0
    n = 0
    while u < e_len:
        d = _Cf(i, n)
        n += 1
        r[u] = d & 255
        u += 1
        if u < e_len:
            r[u] = (d >> 8) & 255
            u += 1
        if u < e_len:
            r[u] = (d >> 16) & 255
            u += 1
        if u < e_len:
            r[u] = (d >> 24) & 255
            u += 1
    return r


def _Df(l_str: str) -> bytearray:
    s = l_str.replace("-", "+").replace("_", "/")
    pad = (4 - (len(s) % 4)) % 4
    s += "=" * pad
    return bytearray(base64.b64decode(s))


def decrypt_speedrace_payload(enc_text: str, seed: str, tmdb_id: int) -> str:
    i = _Df(enc_text)
    r = _xf(seed, tmdb_id, len(i))
    for n in range(len(i)):
        i[n] ^= r[n]
    for n in range(len(_YS)):
        if i[n] != _YS[n]:
            raise ValueError("decrypt failed: bad seed or tampered payload")
    return i[len(_YS):].decode("utf-8")


def get_speedrace_seed(tmdb_id: int) -> str:
    now = time.time()
    with _SEED_LOCK:
        cached = _SEED_CACHE.get(tmdb_id)
        if cached and cached[1] > now:
            return cached[0]

    headers = {
        "User-Agent": DEFAULT_HEADERS["User-Agent"],
        "Referer": "https://www.vidking.net/",
    }
    url = f"https://api.speedracelight.com/seed?mediaId={tmdb_id}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    seed = data["seed"]
    ttl = data.get("ttlMs", 30000) / 1000.0
    with _SEED_LOCK:
        _SEED_CACHE[tmdb_id] = (seed, now + max(10.0, ttl - 5.0))
    return seed


def extract_speedrace_stream(
    tmdb_id: int,
    title: str = "",
    media_type: str = "movie",
    season: int = 1,
    episode: int = 1,
    year: str = "",
) -> Dict[str, Any]:
    """
    Extract HLS stream information from Server 5 (VidKing / SpeedRaceLight).
    This provider operates an open CORS CDN with no Cloudflare WAF datacenter blocks.
    """
    if not title or not year:
        try:
            tmdb_endpoint = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}?api_key={TMDB_API_KEY}"
            t_data = _http_get_json(tmdb_endpoint)
            title = title or t_data.get("title") or t_data.get("name") or "Video"
            rel = t_data.get("release_date") or t_data.get("first_air_date") or ""
            year = year or (rel[:4] if rel else "2024")
        except Exception:
            title = title or "Video"
            year = year or "2024"

    seed = get_speedrace_seed(tmdb_id)
    headers = {
        "User-Agent": DEFAULT_HEADERS["User-Agent"],
        "Referer": "https://www.vidking.net/",
        "Cache-Control": "no-cache, no-store, must-revalidate",
    }

    endpoints = [
        "https://api.speedracelight.com/cdn/sources-with-title",
        "https://api.speedracelight.com/downloader2/sources-with-title",
        "https://api.speedracelight.com/m4uhd/sources-with-title",
        "https://api.speedracelight.com/vsrc/sources-with-title",
        "https://api.speedracelight.com/superflix/sources-with-title",
    ]

    last_error = None
    for ep_url in endpoints:
        params = urllib.parse.urlencode({
            "title": title,
            "mediaType": media_type,
            "year": str(year),
            "episodeId": str(episode if media_type == "tv" else 1),
            "seasonId": str(season if media_type == "tv" else 1),
            "tmdbId": str(tmdb_id),
            "enc": "2",
            "seed": seed,
            "_t": str(int(time.time() * 1000)),
        })
        full_url = f"{ep_url}?{params}"
        try:
            req = urllib.request.Request(full_url, headers=headers)
            with urllib.request.urlopen(req, timeout=8) as resp:
                enc_text = resp.read().decode("utf-8", errors="replace")
            dec_str = decrypt_speedrace_payload(enc_text, seed, tmdb_id)
            payload = json.loads(dec_str)
            sources = payload.get("sources", [])
            if sources:
                video_qualities = []
                for s in sources:
                    q_lbl = s.get("quality", "1080p")
                    q_lower = q_lbl.lower()
                    res = "1920x1080"
                    bw = 4500000
                    if "2160" in q_lower or "4k" in q_lower:
                        res = "3840x2160"
                        bw = 12000000
                    elif "1080" in q_lower:
                        res = "1920x1080"
                        bw = 4500000
                    elif "720" in q_lower:
                        res = "1280x720"
                        bw = 2000000
                    elif "480" in q_lower:
                        res = "854x480"
                        bw = 1000000
                    elif "360" in q_lower:
                        res = "640x360"
                        bw = 600000

                    video_qualities.append({
                        "resolution": res,
                        "label": q_lbl,
                        "bandwidth": bw,
                        "url": s.get("url"),
                    })

                video_qualities.sort(key=lambda x: x["bandwidth"], reverse=True)
                master_url = payload.get("playlist") or video_qualities[0]["url"]

                return {
                    "server": "Server 5 (VidKing)",
                    "master_playlist_url": master_url,
                    "video_qualities": video_qualities,
                    "audio_tracks": [{"name": "English", "language": "eng"}],
                    "subtitles": payload.get("subtitles", []),
                    "http_headers": {
                        "Referer": "https://www.vidking.net/",
                        "User-Agent": DEFAULT_HEADERS["User-Agent"],
                    },
                }
        except Exception as e:
            last_error = e
            continue

    raise ValueError(f"Could not extract stream from SpeedRace servers: {last_error}")


def extract_stream(
    tmdb_id: int,
    media_type: str = "movie",
    season: int = 1,
    episode: int = 1,
    title: str = "",
    year: str = "",
) -> Dict[str, Any]:
    """
    Unified multi-server stream extraction.
    Tries Server 5 (VidKing) first (cloud-optimized, no WAF blocks),
    then falls back to Server 1 (VixSrc).
    """
    if tmdb_id == DEMO_MEDIA_ID:
        return {
            "server": "Demo Stream Server (Mux Open HLS)",
            "master_playlist_url": "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8",
            "video_qualities": [
                {"resolution": "1920x1080", "label": "1080p", "bandwidth": 6221600},
                {"resolution": "1280x720", "label": "720p", "bandwidth": 2149280},
                {"resolution": "848x480", "label": "480p", "bandwidth": 836280},
            ],
            "audio_tracks": [{"name": "English", "language": "eng"}],
            "subtitles": [],
            "http_headers": {
                "User-Agent": DEFAULT_HEADERS["User-Agent"],
            },
        }

    # Attempt 1: Server 5 (VidKing / SpeedRaceLight)
    try:
        res = extract_speedrace_stream(
            tmdb_id=tmdb_id,
            title=title,
            media_type=media_type,
            season=season,
            episode=episode,
            year=year,
        )
        if res and res.get("master_playlist_url"):
            return res
    except Exception as ex_sr:
        logger.info(f"Server 5 (VidKing) extraction failed for {tmdb_id}: {ex_sr}. Falling back to Server 1...")

    # Attempt 2: Server 1 (VixSrc)
    try:
        return extract_vixsrc_stream(tmdb_id, media_type, season, episode)
    except Exception as ex_vix:
        logger.warning(f"Server 1 (VixSrc) extraction failed for {tmdb_id}: {ex_vix}")
        raise ValueError(
            f"Could not extract stream for this title across streaming servers. "
            f"Please try another episode or title, or test downloading using the Demo Showcase."
        )


def search_media(query: str) -> List[Dict[str, Any]]:
    """
    Search HDTodayz and TMDB for matching titles.
    """
    query = query.strip()
    if not query:
        return []

    encoded = urllib.parse.quote(query)
    endpoint = f"https://api.themoviedb.org/3/search/multi?api_key={TMDB_API_KEY}&query={encoded}&include_adult=false"

    try:
        data = _http_get_json(endpoint)
        results = []
        for item in data.get("results", []):
            m_type = item.get("media_type")
            if m_type not in ("movie", "tv"):
                continue

            tmdb_id = item.get("id")
            title = item.get("title") or item.get("name") or "Untitled"
            rel_date = item.get("release_date") or item.get("first_air_date") or ""
            year = rel_date[:4] if rel_date else ""
            poster_path = item.get("poster_path")
            poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else ""
            backdrop_path = item.get("backdrop_path")
            backdrop_url = f"https://image.tmdb.org/t/p/w1280{backdrop_path}" if backdrop_path else ""

            vote_count = item.get("vote_count", 0)
            popularity = item.get("popularity", 0.0)

            results.append({
                "id": tmdb_id,
                "title": title,
                "media_type": m_type,
                "year": year,
                "release_date": rel_date,
                "overview": item.get("overview") or "",
                "poster_url": poster_url,
                "backdrop_url": backdrop_url,
                "rating": round(item.get("vote_average", 0), 1),
                "vote_count": vote_count,
                "popularity": popularity,
                "hdtoday_url": f"https://hdtodayz.org/watch/{m_type}/{tmdb_id}",
            })

        # Rank results: prioritize exact title match, established release (>15 votes), popularity, and vote count
        q_norm = query.strip().lower()
        results.sort(
            key=lambda x: (
                x["title"].strip().lower() == q_norm,
                x.get("vote_count", 0) > 15,
                x.get("popularity", 0.0),
                x.get("vote_count", 0),
            ),
            reverse=True,
        )

        if any(k in q_norm for k in ("demo", "test", "bunny", "sample", "showcase")):
            demo_search_item = {
                "id": DEMO_MEDIA_ID,
                "title": "Big Buck Bunny (HLS Test Showcase)",
                "media_type": "tv",
                "year": "2024",
                "release_date": "2024-01-01",
                "overview": "Official 100% reliable demo stream for testing speed and simultaneous downloads.",
                "poster_url": "https://upload.wikimedia.org/wikipedia/commons/thumb/c/c5/Big_buck_bunny_poster_big.jpg/600px-Big_buck_bunny_poster_big.jpg",
                "backdrop_url": "https://peach.blender.org/wp-content/uploads/bbb-splash.png",
                "rating": 9.9,
                "vote_count": 9999,
                "popularity": 9999.0,
                "hdtoday_url": "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8",
            }
            results.insert(0, demo_search_item)

        return results
    except Exception as e:
        logger.error(f"Search error for '{query}': {e}")
        return []


def get_trending_catalog() -> Dict[str, List[Dict[str, Any]]]:
    """
    Fetch trending movies and TV shows currently featured on HDTodayz / TMDB.
    """
    endpoint = f"https://api.themoviedb.org/3/trending/all/day?api_key={TMDB_API_KEY}"
    try:
        data = _http_get_json(endpoint)
        movies = []
        tv_shows = []

        # Feature Demo Showcase for 100% reliable cloud testing
        demo_entry = {
            "id": DEMO_MEDIA_ID,
            "title": "Big Buck Bunny (🧪 Cloud Test Stream)",
            "media_type": "tv",
            "year": "2024",
            "release_date": "2024-01-01",
            "overview": "Official 100% reliable HLS demo stream. Use this to test high-speed MP4 downloads, simultaneous multi-episode downloads, and real-time remuxing without datacenter blocks.",
            "poster_url": "https://upload.wikimedia.org/wikipedia/commons/thumb/c/c5/Big_buck_bunny_poster_big.jpg/600px-Big_buck_bunny_poster_big.jpg",
            "backdrop_url": "https://peach.blender.org/wp-content/uploads/bbb-splash.png",
            "rating": 9.9,
            "hdtoday_url": "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8",
        }
        tv_shows.append(demo_entry)

        for item in data.get("results", []):
            m_type = item.get("media_type")
            if m_type not in ("movie", "tv"):
                continue

            tmdb_id = item.get("id")
            title = item.get("title") or item.get("name") or "Untitled"
            rel_date = item.get("release_date") or item.get("first_air_date") or ""
            year = rel_date[:4] if rel_date else ""
            poster_path = item.get("poster_path")
            poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else ""
            backdrop_path = item.get("backdrop_path")
            backdrop_url = f"https://image.tmdb.org/t/p/w1280{backdrop_path}" if backdrop_path else ""

            entry = {
                "id": tmdb_id,
                "title": title,
                "media_type": m_type,
                "year": year,
                "release_date": rel_date,
                "overview": item.get("overview") or "",
                "poster_url": poster_url,
                "backdrop_url": backdrop_url,
                "rating": round(item.get("vote_average", 0), 1),
                "hdtoday_url": f"https://hdtodayz.org/watch/{m_type}/{tmdb_id}",
            }

            if m_type == "movie":
                movies.append(entry)
            else:
                tv_shows.append(entry)

        return {"movies": movies, "tv": tv_shows}
    except Exception as e:
        logger.error(f"Error fetching trending catalog: {e}")
        return {"movies": [], "tv": []}
