import os
import re
import uuid
import glob
import asyncio
import subprocess
import urllib.parse
import zipfile
import json
import time
import threading
from copy import deepcopy
from html import unescape
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter
from fastapi import HTTPException
import shutil
import logging
import yt_dlp

logger = logging.getLogger("downloader")

# Ensure mobile clients are not stripped by yt-dlp when cookies are provided
try:
    import yt_dlp.extractor.youtube._video as ytv
    for client in ('android', 'ios', 'visionos', 'android_vr'):
        if client in ytv.INNERTUBE_CLIENTS:
            ytv.INNERTUBE_CLIENTS[client]['SUPPORTS_COOKIES'] = True
except Exception as e:
    logger.warning(f"Could not patch INNERTUBE_CLIENTS cookie support: {e}")

TEMP_DIR = Path(__file__).resolve().parent.parent / "temp"
TEMP_DIR.mkdir(exist_ok=True)

NODE_BIN = shutil.which("node") or shutil.which("nodejs") or shutil.which("deno")
FFMPEG_BIN = shutil.which("ffmpeg")
COOKIES_FILE = Path(__file__).resolve().parent.parent / "cookies.txt"

# -------------------------------------------------------------------------
# Global Persistent HTTP Session & Connection Pool for high performance
# -------------------------------------------------------------------------
_HTTP_SESSION: Optional[requests.Session] = None

def get_http_session() -> requests.Session:
    global _HTTP_SESSION
    if _HTTP_SESSION is None:
        _HTTP_SESSION = requests.Session()
        adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=2)
        _HTTP_SESSION.mount("http://", adapter)
        _HTTP_SESSION.mount("https://", adapter)
    return _HTTP_SESSION

# -------------------------------------------------------------------------
# In-Memory TTL Cache for Media Info (eliminates double extractions)
# -------------------------------------------------------------------------
_MEDIA_INFO_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_LOCK = threading.Lock()
CACHE_TTL_SECONDS = 900  # 15 minutes

def get_cached_media_info(url: str, platform: str) -> Optional[Dict[str, Any]]:
    key = f"{platform}:{url.strip()}"
    now = time.time()
    with _CACHE_LOCK:
        if key in _MEDIA_INFO_CACHE:
            item = _MEDIA_INFO_CACHE[key]
            if now - item["timestamp"] < CACHE_TTL_SECONDS:
                return deepcopy(item["data"])
            else:
                del _MEDIA_INFO_CACHE[key]
    return None

def set_cached_media_info(url: str, platform: str, data: Dict[str, Any]) -> None:
    key = f"{platform}:{url.strip()}"
    with _CACHE_LOCK:
        _MEDIA_INFO_CACHE[key] = {
            "timestamp": time.time(),
            "data": deepcopy(data)
        }

def get_base_ydl_opts() -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 15,
        'concurrent_fragment_downloads': 4,
        'remote_components': ['ejs:github'],
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
        }
    }
    
    cookies_env = os.environ.get("YOUTUBE_COOKIES")
    if cookies_env and cookies_env.strip():
        env_cookie_path = TEMP_DIR / "env_cookies.txt"
        cleaned_cookies = cookies_env.replace('\\n', '\n').strip()
        if not env_cookie_path.exists() or env_cookie_path.read_text(encoding="utf-8", errors="ignore") != cleaned_cookies:
            env_cookie_path.write_text(cleaned_cookies, encoding="utf-8")
        opts['cookiefile'] = str(env_cookie_path)
    elif COOKIES_FILE.exists():
        opts['cookiefile'] = str(COOKIES_FILE)

    opts['extractor_args'] = {
        'youtube': {
            'formats': ['missing_pot'],
            'player_client': ['web', 'visionos', 'android'] if 'cookiefile' in opts else ['visionos', 'android']
        }
    }

    deno_bin = shutil.which("deno")
    node_bin = shutil.which("node") or shutil.which("nodejs")
    if deno_bin:
        opts['js_runtimes'] = {'deno': {'path': deno_bin}}
    elif node_bin:
        opts['js_runtimes'] = {'node': {'path': node_bin}}
    else:
        opts['js_runtimes'] = {'node': {}}
    if FFMPEG_BIN:
        opts['ffmpeg_location'] = FFMPEG_BIN
    return opts

def format_duration(seconds: Optional[int]) -> str:
    """Format duration in seconds into mm:ss or hh:mm:ss"""
    if not seconds or seconds <= 0:
        return "N/A"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def stream_download_to_file(download_url: str, target_path: Path, headers: Optional[Dict[str, str]] = None, max_size: int = 150 * 1024 * 1024) -> None:
    """Streams a remote media file directly to disk with multi-tier fallback for CDN anti-bot / 403."""
    clean_url = unescape(download_url).replace('\\/', '/').replace('\\u0026', '&').replace('&amp;', '&').strip()
    
    parsed = urllib.parse.urlparse(clean_url)
    hostname = (parsed.hostname or "").lower()
    is_cdn = any(cdn in hostname for cdn in ("fbcdn.net", "cdninstagram.com", "tiktokcdn.com", "byteoversea.com", "googlevideo.com"))
    
    user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
    
    attempts: List[Dict[str, Any]] = []
    
    # Tier 1: Clean request without cookies, clean browser UA, NO Referer for CDN
    tier1_headers = {'User-Agent': user_agent, 'Accept': '*/*', 'Accept-Encoding': 'identity'}
    if not is_cdn and headers and 'Referer' in headers:
        tier1_headers['Referer'] = headers['Referer']
    attempts.append({'headers': tier1_headers, 'use_session': False})
    
    # Tier 2: With caller-supplied headers (if any), without session cookies
    if headers:
        tier2_headers = dict(headers)
        if 'User-Agent' not in tier2_headers:
            tier2_headers['User-Agent'] = user_agent
        attempts.append({'headers': tier2_headers, 'use_session': False})
        
    # Tier 3: Bare request without custom headers
    attempts.append({'headers': None, 'use_session': False})
    
    # Tier 4: Using persistent session (only for non-CDN domains)
    if not is_cdn:
        attempts.append({'headers': headers or {'User-Agent': user_agent}, 'use_session': True})

    last_status = 0
    success = False
    
    for attempt in attempts:
        req_headers = attempt['headers']
        use_sess = attempt['use_session']
        try:
            if use_sess:
                req_obj = get_http_session()
                resp = req_obj.get(clean_url, headers=req_headers, stream=True, timeout=25)
            else:
                resp = requests.get(clean_url, headers=req_headers, stream=True, timeout=25)
                
            last_status = resp.status_code
            if resp.status_code in (200, 206):
                bytes_written = 0
                with open(target_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            bytes_written += len(chunk)
                            if bytes_written > max_size:
                                raise HTTPException(status_code=413, detail="File media melebihi batas ukuran maksimum 150MB.")
                            f.write(chunk)
                if target_path.exists() and target_path.stat().st_size > 0:
                    success = True
                    break
        except HTTPException:
            if target_path.exists():
                try:
                    target_path.unlink()
                except OSError:
                    pass
            raise
        except Exception as e:
            logger.warning(f"Download attempt failed for {clean_url[:60]}: {e}")
            if target_path.exists():
                try:
                    target_path.unlink()
                except OSError:
                    pass

    # Tier 5: yt-dlp fallback if direct HTTP attempts failed
    if not success:
        logger.info(f"Direct HTTP attempts failed (last status: {last_status}). Trying yt-dlp direct stream...")
        try:
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'outtmpl': str(target_path),
                'socket_timeout': 20,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([clean_url])
            if target_path.exists() and target_path.stat().st_size > 0:
                success = True
        except Exception as yt_err:
            logger.warning(f"yt-dlp stream download also failed: {yt_err}")

    if not success:
        if target_path.exists():
            try:
                target_path.unlink()
            except OSError:
                pass
        status_msg = f" (Status {last_status})" if last_status else ""
        raise HTTPException(status_code=400, detail=f"Gagal mengunduh file media dari penyedia{status_msg}.")

def convert_to_mp3_direct(input_path: Path, output_path: Path, bitrate: str = "320") -> None:
    """
    Direct FFmpeg MP3 conversion with multi-encoder fallbacks:
    1. Try libmp3lame with explicit audio mapping (-map 0:a:0?)
    2. Try mp3 (Built-in)
    3. Try auto-selected encoder
    """
    ffmpeg_cmd = FFMPEG_BIN or "ffmpeg"
    valid_bitrate = bitrate if bitrate in ("128", "192", "256", "320") else "320"
    
    attempts = [
        [ffmpeg_cmd, "-y", "-i", str(input_path.resolve()), "-map", "0:a:0?", "-vn", "-c:a", "libmp3lame", "-b:a", f"{valid_bitrate}k", str(output_path.resolve())],
        [ffmpeg_cmd, "-y", "-i", str(input_path.resolve()), "-map", "0:a:0?", "-vn", "-c:a", "mp3", "-b:a", f"{valid_bitrate}k", str(output_path.resolve())],
        [ffmpeg_cmd, "-y", "-i", str(input_path.resolve()), "-vn", "-b:a", f"{valid_bitrate}k", str(output_path.resolve())]
    ]

    last_stderr = ""
    for cmd in attempts:
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            if res.returncode == 0 and output_path.exists() and output_path.stat().st_size > 1000:
                return
            last_stderr = res.stderr.decode("utf-8", errors="ignore")
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="Proses konversi audio memakan waktu terlalu lama (timeout).")
        except Exception as e:
            last_stderr = str(e)

    if "does not contain any stream" in last_stderr or "Output file is empty" in last_stderr:
        logger.warning(f"File {input_path} does not have an audio stream.")
        raise HTTPException(status_code=400, detail="Media ini tidak memiliki trek audio (suara).")

    logger.error(f"FFmpeg MP3 conversion failed. Stderr tail: {last_stderr[-500:]}")
    raise HTTPException(status_code=500, detail="Gagal mengonversi audio ke MP3.")

def create_zip_from_media_items(media_items: List[Dict[str, Any]], download_id: str, headers: Optional[Dict[str, str]] = None) -> Path:
    """
    Downloads all media items (images and videos) concurrently in parallel,
    and packages them into a single ZIP archive.
    """
    zip_path = TEMP_DIR / f"{download_id}.zip"
    temp_files: List[Path] = []
    
    def download_one(idx: int, item: Dict[str, Any]) -> Optional[Tuple[int, Path, str]]:
        item_url = item.get("url")
        if not item_url:
            return None
        m_type = item.get("type", "image")
        ext = "mp4" if m_type == "video" else "jpg"
        arc_name = f"slide_{idx+1:02d}.{ext}"
        tmp_file = TEMP_DIR / f"{download_id}_slide_{idx+1}.{ext}"
        candidate_urls = item.get("video_urls") or ([item_url] if item_url else [])
        for c_url in candidate_urls:
            try:
                stream_download_to_file(c_url, tmp_file, headers=headers)
                if tmp_file.exists() and tmp_file.stat().st_size > 0:
                    return (idx, tmp_file, arc_name)
            except Exception as e:
                logger.warning(f"Download attempt failed for carousel item #{idx+1} from {c_url[:50]}: {e}")
        return None

    # Execute concurrent downloads (up to 6 parallel workers for speed)
    results = []
    max_workers = min(6, max(len(media_items), 1))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(download_one, i, item) for i, item in enumerate(media_items)]
        for fut in as_completed(futures):
            res = fut.result()
            if res:
                results.append(res)
                temp_files.append(res[1])

    results.sort(key=lambda x: x[0])
    if not results:
        raise HTTPException(status_code=500, detail="Gagal mengunduh file media untuk arsip ZIP.")

    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zip_f:
            for idx, tmp_file, arc_name in results:
                zip_f.write(tmp_file, arcname=arc_name)
    finally:
        for f in temp_files:
            if f.exists():
                try:
                    f.unlink()
                except OSError:
                    pass

    return zip_path

def create_zip_from_urls(image_urls: List[str], download_id: str, headers: Optional[Dict[str, str]] = None) -> Path:
    """Helper converting raw URL list to media items for ZIP packaging."""
    items = [{"type": "image", "url": u} for u in image_urls]
    return create_zip_from_media_items(items, download_id, headers=headers)

# =========================================================================
# Threads Dedicated Handler (Universal Video, Image & Mixed Carousel)
# =========================================================================
def extract_og_tag(html: str, tag_name: str) -> Optional[str]:
    p1 = rf'<meta[^>]+(?:property|name)=["\']{re.escape(tag_name)}["\'][^>]+content=["\']([^"\']+)["\']'
    p2 = rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']{re.escape(tag_name)}["\']'
    m = re.search(p1, html, re.IGNORECASE) or re.search(p2, html, re.IGNORECASE)
    return unescape(m.group(1).strip()) if m else None

def extract_threads_info(url: str) -> Dict[str, Any]:
    browser_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-User': '?1',
        'Sec-Fetch-Dest': 'document',
    }
    
    session = get_http_session()
    html = ""
    resp_url = url
    try:
        resp = session.get(url, headers=browser_headers, allow_redirects=True, timeout=15)
        if resp.status_code == 200:
            html = resp.text
            resp_url = str(resp.url)
    except Exception as e:
        logger.warning(f"Browser request failed for Threads: {e}")

    # Fallback to crawler UA if initial request failed or was blocked
    if not html:
        crawler_headers = {'User-Agent': 'facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)'}
        try:
            resp = session.get(url, headers=crawler_headers, allow_redirects=True, timeout=15)
            html = resp.text
            resp_url = str(resp.url)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Gagal mengakses halaman Threads: {str(e)}")

    title = extract_og_tag(html, "og:title") or "Threads Post"
    uploader = "Threads User"
    user_match = re.search(r'@([a-zA-Z0-9._]+)', resp_url) or re.search(r'@([a-zA-Z0-9._]+)', title)
    if user_match:
        uploader = user_match.group(1)

    media_items: List[Dict[str, Any]] = []
    primary_video_url = ""
    primary_image_url = ""

    # Method 1: Deep JSON search in <script type="application/json">
    scripts = re.findall(r'<script type="application/json"[^>]*>(.*?)</script>', html)
    for s in scripts:
        if 'video_versions' in s or 'carousel_media' in s or 'image_versions2' in s:
            try:
                data = json.loads(s)
                def search_media_node(o):
                    if isinstance(o, dict):
                        if ('carousel_media' in o or 'video_versions' in o or 'image_versions2' in o) and (o.get('pk') or o.get('id')):
                            return o
                        for v in o.values():
                            res = search_media_node(v)
                            if res:
                                return res
                    elif isinstance(o, list):
                        for x in o:
                            res = search_media_node(x)
                            if res:
                                return res
                    return None
                
                node = search_media_node(data)
                if node:
                    if node.get("caption") and isinstance(node["caption"], dict) and node["caption"].get("text"):
                        title = node["caption"]["text"][:100]
                    if node.get("user") and isinstance(node["user"], dict) and node["user"].get("username"):
                        uploader = node["user"]["username"]
                        
                    if "carousel_media" in node and node["carousel_media"]:
                        for idx, itm in enumerate(node["carousel_media"]):
                            if itm.get("video_versions"):
                                all_v_urls = [v["url"] for v in itm["video_versions"] if v.get("url")]
                                v_url = all_v_urls[0] if all_v_urls else ""
                                t_url = (itm.get("image_versions2", {}).get("candidates") or [{}])[0].get("url") or ""
                                media_items.append({
                                    "index": idx + 1,
                                    "type": "video",
                                    "url": v_url,
                                    "thumbnail": t_url,
                                    "width": itm.get("original_width"),
                                    "height": itm.get("original_height"),
                                    "video_urls": all_v_urls
                                })
                                if not primary_video_url and v_url:
                                    primary_video_url = v_url
                            elif itm.get("image_versions2"):
                                cands = itm["image_versions2"].get("candidates") or []
                                if cands:
                                    img_url = cands[0]["url"]
                                    media_items.append({
                                        "index": idx + 1,
                                        "type": "image",
                                        "url": img_url,
                                        "thumbnail": img_url,
                                        "width": itm.get("original_width"),
                                        "height": itm.get("original_height")
                                    })
                                    if not primary_image_url:
                                        primary_image_url = img_url
                    elif node.get("video_versions"):
                        all_v_urls = [v["url"] for v in node["video_versions"] if v.get("url")]
                        v_url = all_v_urls[0] if all_v_urls else ""
                        t_url = (node.get("image_versions2", {}).get("candidates") or [{}])[0].get("url") or ""
                        media_items.append({
                            "index": 1,
                            "type": "video",
                            "url": v_url,
                            "thumbnail": t_url,
                            "video_urls": all_v_urls
                        })
                        primary_video_url = v_url
                    elif node.get("image_versions2"):
                        cands = node["image_versions2"].get("candidates") or []
                        if cands:
                            img_url = cands[0]["url"]
                            media_items.append({
                                "index": 1,
                                "type": "image",
                                "url": img_url,
                                "thumbnail": img_url
                            })
                            primary_image_url = img_url
                    if media_items:
                        break
            except Exception:
                pass

    # Method 2: Fallback to regex matching if JSON extraction didn't populate items
    if not media_items:
        clean_html = unescape(html).replace('\\/', '/').replace('\\u0026', '&').replace('&amp;', '&')
        raw_video_matches = re.findall(r'https:[^"\'\s<>]*(?:fbcdn\.net|cdninstagram\.com)[^"\'\s<>]*\.mp4[^"\'\s<>]*', clean_html)
        valid_video_matches = [
            v for v in raw_video_matches
            if 'static.cdninstagram.com' not in v and 'rsrc.php' not in v and '/assets/' not in v
        ]
        if valid_video_matches:
            primary_video_url = valid_video_matches[0]
            media_items.append({
                "index": 1,
                "type": "video",
                "url": primary_video_url,
                "thumbnail": "",
                "video_urls": valid_video_matches
            })

        photo_matches = re.findall(r'https:[^"\'\s<>]*(?:fbcdn\.net|cdninstagram\.com)[^"\'\s<>]*t51\.82787-15[^"\'\s<>]*', clean_html)
        by_media_id: Dict[str, List[str]] = {}
        for m in photo_matches:
            if 'static.cdninstagram.com' in m or 'rsrc.php' in m:
                continue
            match_id = re.search(r'_(\d{15,20})_', m)
            if match_id:
                mid = match_id.group(1)
                by_media_id.setdefault(mid, []).append(m)

        for mid, urls in by_media_id.items():
            best = urls[0]
            for u in urls:
                if '1080' in u or '1440' in u:
                    best = u
                    break
            media_items.append({
                "index": len(media_items) + 1,
                "type": "image",
                "url": best,
                "thumbnail": best
            })

    # Method 3: Fallback to OpenGraph
    if not media_items:
        og_vid = extract_og_tag(html, "og:video") or extract_og_tag(html, "og:video:secure_url")
        if og_vid and 'static.cdninstagram.com' not in og_vid and 'rsrc.php' not in og_vid:
            primary_video_url = og_vid
            media_items.append({"index": 1, "type": "video", "url": og_vid, "thumbnail": ""})
        else:
            og_img = extract_og_tag(html, "og:image") or extract_og_tag(html, "og:image:secure_url")
            if og_img and 'static.cdninstagram.com' not in og_img and 'rsrc.php' not in og_img:
                primary_image_url = og_img
                media_items.append({"index": 1, "type": "image", "url": og_img, "thumbnail": og_img})

    if not media_items:
        raise HTTPException(
            status_code=404,
            detail="Tidak dapat menemukan media pada postingan Threads ini. Pastikan link aktif, memiliki media, dan akun bersifat publik."
        )

    # Determine media attributes
    has_video = any(m["type"] == "video" for m in media_items)
    has_audio = has_video
    is_image = all(m["type"] == "image" for m in media_items) and len(media_items) > 0
    thumbnail = ""
    for m in media_items:
        if m.get("thumbnail"):
            thumbnail = m["thumbnail"]
            break
        elif m["type"] == "image" and m.get("url"):
            thumbnail = m["url"]
            break
    if not thumbnail:
        thumbnail = extract_og_tag(html, "og:image") or ""

    if not primary_video_url and has_video:
        for m in media_items:
            if m["type"] == "video":
                primary_video_url = m["url"]
                break

    video_qualities = []
    if has_video:
        video_qualities.append({
            "quality": "best",
            "resolution": "Original (MP4)",
            "label": "Original (Kualitas Penuh)",
            "size_mb": None
        })

    audio_qualities = []
    if has_audio:
        audio_qualities = [
            {"bitrate": "320", "label": "320 kbps (Studio HQ)", "size_mb": None},
            {"bitrate": "192", "label": "192 kbps (Standar)", "size_mb": None},
            {"bitrate": "128", "label": "128 kbps (Ringan)", "size_mb": None}
        ]

    all_post_video_urls = []
    for m in media_items:
        if m.get("type") == "video":
            if m.get("video_urls"):
                all_post_video_urls.extend(m["video_urls"])
            elif m.get("url"):
                all_post_video_urls.append(m["url"])
    if not all_post_video_urls and primary_video_url:
        all_post_video_urls.append(primary_video_url)

    return {
        "title": title,
        "uploader": uploader,
        "duration": "N/A",
        "duration_seconds": None,
        "thumbnail": thumbnail,
        "platform": "threads",
        "has_video": has_video,
        "has_audio": has_audio,
        "is_image": is_image,
        "image_urls": [m["url"] for m in media_items if m["type"] == "image"],
        "media_items": media_items,
        "video_qualities": video_qualities,
        "audio_qualities": audio_qualities,
        "url": url,
        "video_url": primary_video_url,
        "video_urls": all_post_video_urls,
    }

def download_threads_media(url: str, format_type: str, download_id: str, quality: Optional[str] = None, slide_index: Optional[int] = None) -> Dict[str, Any]:
    info = get_cached_media_info(url, "threads") or extract_threads_info(url)
    title = info["title"]
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
        'Referer': 'https://www.threads.net/'
    }
    media_items = info.get("media_items") or []

    # 1. ZIP download for all media items (images and videos)
    if format_type == "zip" or (format_type == "image" and slide_index is None and len(media_items) > 1):
        if not media_items:
            raise HTTPException(status_code=404, detail="Tidak ada media yang ditemukan untuk diunduh.")
        zip_path = create_zip_from_media_items(media_items, download_id, headers=headers)
        return {
            "file_path": zip_path,
            "title": f"{title}_all_media",
            "ext": "zip",
            "mime_type": "application/zip"
        }

    # 2. Slide-specific download
    if slide_index is not None and slide_index > 0 and media_items:
        target_idx = min(slide_index - 1, len(media_items) - 1)
        target_item = media_items[target_idx]
        target_type = target_item.get("type", "image")
        target_url = target_item.get("url")

        if target_type == "video":
            video_candidates = target_item.get("video_urls") or ([target_url] if target_url else [])
            raw_path = TEMP_DIR / f"raw_{download_id}.mp4"
            dl_err = None
            for c_url in video_candidates:
                try:
                    stream_download_to_file(c_url, raw_path, headers=headers)
                    if raw_path.exists() and raw_path.stat().st_size > 0:
                        dl_err = None
                        break
                except Exception as e:
                    dl_err = e
            if dl_err:
                raise dl_err

            if format_type == "mp3":
                mp3_path = TEMP_DIR / f"{download_id}.mp3"
                convert_to_mp3_direct(raw_path, mp3_path, bitrate=quality or "320")
                try:
                    raw_path.unlink()
                except OSError:
                    pass
                return {
                    "file_path": mp3_path,
                    "title": f"{title}_slide_{target_idx+1}",
                    "ext": "mp3",
                    "mime_type": "audio/mpeg"
                }
            else:  # mp4
                return {
                    "file_path": raw_path,
                    "title": f"{title}_slide_{target_idx+1}",
                    "ext": "mp4",
                    "mime_type": "video/mp4"
                }
        else:  # image
            out_path = TEMP_DIR / f"{download_id}.jpg"
            stream_download_to_file(target_url, out_path, headers=headers)
            return {
                "file_path": out_path,
                "title": f"{title}_slide_{target_idx+1}",
                "ext": "jpg",
                "mime_type": "image/jpeg"
            }

    # 3. Non-slide specific download
    if format_type == "image":
        img_urls = info.get("image_urls") or [m["url"] for m in media_items if m["type"] == "image"]
        if not img_urls:
            raise HTTPException(status_code=404, detail="Gambar tidak ditemukan pada postingan Threads ini.")
        out_path = TEMP_DIR / f"{download_id}.jpg"
        stream_download_to_file(img_urls[0], out_path, headers=headers)
        return {
            "file_path": out_path,
            "title": title,
            "ext": "jpg",
            "mime_type": "image/jpeg"
        }

    elif format_type in ("mp4", "mp3"):
        video_candidates = []
        if info.get("video_url"):
            video_candidates.append(info["video_url"])
        for v in (info.get("video_urls") or []):
            if v not in video_candidates:
                video_candidates.append(v)
        if not video_candidates:
            for m in media_items:
                if m.get("type") == "video":
                    v_cands = m.get("video_urls") or ([m["url"]] if m.get("url") else [])
                    for v in v_cands:
                        if v not in video_candidates:
                            video_candidates.append(v)
                    if video_candidates:
                        break
        if not video_candidates:
            raise HTTPException(status_code=400, detail="Tidak ada video pada postingan Threads ini.")

        raw_path = TEMP_DIR / f"raw_{download_id}.mp4"
        dl_err = None
        for c_url in video_candidates:
            try:
                stream_download_to_file(c_url, raw_path, headers=headers)
                if raw_path.exists() and raw_path.stat().st_size > 0:
                    dl_err = None
                    break
            except Exception as e:
                dl_err = e
        if dl_err:
            raise dl_err

        if format_type == "mp4":
            return {
                "file_path": raw_path,
                "title": title,
                "ext": "mp4",
                "mime_type": "video/mp4"
            }
        else:
            mp3_path = TEMP_DIR / f"{download_id}.mp3"
            convert_to_mp3_direct(raw_path, mp3_path, bitrate=quality or "320")
            try:
                raw_path.unlink()
            except OSError:
                pass
            return {
                "file_path": mp3_path,
                "title": title,
                "ext": "mp3",
                "mime_type": "audio/mpeg"
            }
    else:
        raise HTTPException(status_code=400, detail="Format tidak didukung untuk Threads.")

# =========================================================================
# TikTok Dedicated Handler (TikWM Watermark-Free + Fallback)
# =========================================================================
def extract_tiktok_info(url: str) -> Dict[str, Any]:
    api_url = "https://www.tikwm.com/api/"
    headers = {'User-Agent': 'Mozilla/5.0'}
    session = get_http_session()
    
    try:
        resp = session.post(api_url, data={'url': url, 'count': 12, 'cursor': 0, 'web': 1, 'hd': 1}, headers=headers, timeout=12)
        if resp.status_code == 200:
            res_json = resp.json()
            if res_json.get("code") == 0:
                d = res_json.get("data", {})
                title = d.get("title") or "TikTok Video"
                uploader = d.get("author", {}).get("nickname") or d.get("author", {}).get("unique_id") or "TikTok User"
                duration = d.get("duration", 0)
                thumbnail = d.get("cover") or d.get("origin_cover") or ""
                images = d.get("images") or []
                is_image = len(images) > 0
                has_video = not is_image
                has_audio = True

                video_qualities = []
                if has_video:
                    video_qualities.append({
                        "quality": "best",
                        "resolution": "HD No-Watermark",
                        "label": "Tanpa Watermark (HD)",
                        "size_mb": round(d.get("size", 0) / (1024 * 1024), 1) if d.get("size") else None
                    })

                audio_qualities = [
                    {"bitrate": "320", "label": "320 kbps (Studio HQ)", "size_mb": None},
                    {"bitrate": "192", "label": "192 kbps (Standar)", "size_mb": None},
                    {"bitrate": "128", "label": "128 kbps (Ringan)", "size_mb": None}
                ]

                media_items = []
                if is_image:
                    for i, img in enumerate(images):
                        media_items.append({
                            "index": i + 1,
                            "type": "image",
                            "url": img,
                            "thumbnail": img
                        })
                elif has_video:
                    media_items.append({
                        "index": 1,
                        "type": "video",
                        "url": d.get("play") or d.get("wmplay") or "",
                        "thumbnail": thumbnail
                    })

                return {
                    "title": title,
                    "uploader": uploader,
                    "duration": format_duration(duration),
                    "duration_seconds": duration,
                    "thumbnail": thumbnail,
                    "platform": "tiktok",
                    "has_video": has_video,
                    "has_audio": has_audio,
                    "is_image": is_image,
                    "image_urls": images,
                    "media_items": media_items,
                    "video_qualities": video_qualities,
                    "audio_qualities": audio_qualities,
                    "url": url,
                    "play_url": d.get("play"),
                    "music_url": d.get("music"),
                }
    except Exception as e:
        logger.warning(f"TikWM primary extraction failed: {e}")

    return extract_media_info_ytdlp(url, "tiktok")

def download_tiktok_media(url: str, format_type: str, download_id: str, quality: Optional[str] = None, slide_index: Optional[int] = None) -> Dict[str, Any]:
    api_url = "https://www.tikwm.com/api/"
    headers = {'User-Agent': 'Mozilla/5.0'}
    session = get_http_session()

    try:
        resp = session.post(api_url, data={'url': url, 'count': 12, 'cursor': 0, 'web': 1, 'hd': 1}, headers=headers, timeout=12)
        if resp.status_code == 200:
            res_json = resp.json()
            if res_json.get("code") == 0:
                d = res_json.get("data", {})
                title = d.get("title") or "TikTok Video"
                images = d.get("images") or []

                if format_type == "zip" or (format_type == "image" and slide_index is None and len(images) > 1):
                    if not images:
                        raise HTTPException(status_code=404, detail="Tidak ada gambar pada postingan TikTok ini.")
                    zip_path = create_zip_from_urls(images, download_id)
                    return {
                        "file_path": zip_path,
                        "title": f"{title}_all_photos",
                        "ext": "zip",
                        "mime_type": "application/zip"
                    }

                elif format_type == "image":
                    if not images:
                        raise HTTPException(status_code=404, detail="Gambar tidak ditemukan pada postingan TikTok ini.")
                    target_idx = 0
                    if slide_index is not None and slide_index > 0:
                        target_idx = min(slide_index - 1, len(images) - 1)
                    img_url = images[target_idx]
                    out_path = TEMP_DIR / f"{download_id}.jpg"
                    stream_download_to_file(img_url, out_path, headers=headers)
                    return {
                        "file_path": out_path,
                        "title": f"{title}_slide_{target_idx+1}",
                        "ext": "jpg",
                        "mime_type": "image/jpeg"
                    }
                elif format_type == "mp4":
                    play_url = d.get("hdplay") or d.get("play") or d.get("wmplay")
                    if not play_url:
                        raise HTTPException(status_code=404, detail="Video TikTok tidak dapat diakses.")
                    out_path = TEMP_DIR / f"{download_id}.mp4"
                    stream_download_to_file(play_url, out_path, headers=headers)
                    return {
                        "file_path": out_path,
                        "title": title,
                        "ext": "mp4",
                        "mime_type": "video/mp4"
                    }
                elif format_type == "mp3":
                    music_url = d.get("music")
                    if music_url:
                        raw_audio = TEMP_DIR / f"raw_{download_id}.audio"
                        stream_download_to_file(music_url, raw_audio, headers=headers)
                        mp3_path = TEMP_DIR / f"{download_id}.mp3"
                        convert_to_mp3_direct(raw_audio, mp3_path, bitrate=quality or "320")
                        try:
                            raw_audio.unlink()
                        except OSError:
                            pass
                        return {
                            "file_path": mp3_path,
                            "title": title,
                            "ext": "mp3",
                            "mime_type": "audio/mpeg"
                        }
                    else:
                        play_url = d.get("play") or d.get("wmplay")
                        if not play_url:
                            raise HTTPException(status_code=404, detail="Audio TikTok tidak dapat diekstrak.")
                        raw_vid = TEMP_DIR / f"raw_{download_id}.mp4"
                        stream_download_to_file(play_url, raw_vid, headers=headers)
                        mp3_path = TEMP_DIR / f"{download_id}.mp3"
                        convert_to_mp3_direct(raw_vid, mp3_path, bitrate=quality or "320")
                        try:
                            raw_vid.unlink()
                        except OSError:
                            pass
                        return {
                            "file_path": mp3_path,
                            "title": title,
                            "ext": "mp3",
                            "mime_type": "audio/mpeg"
                        }
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"TikWM download failed ({e}), falling back to standard yt-dlp...")

    return download_media_file_ytdlp(url, format_type, "tiktok", download_id, quality, slide_index)

# =========================================================================
# Standard yt-dlp Handler (YouTube, Instagram, etc.)
# =========================================================================
def extract_media_info_ytdlp(url: str, platform: str) -> Dict[str, Any]:
    """Extracts media metadata using yt-dlp with support for mixed media carousels."""
    ydl_opts = get_base_ydl_opts()
    if platform == "instagram":
        ydl_opts['http_headers']['User-Agent'] = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1'
        ydl_opts['http_headers']['Accept-Language'] = 'en-US,en;q=0.9'
        ydl_opts['http_headers']['X-Forwarded-For'] = '114.122.14.50'
        ydl_opts['http_headers']['Client-IP'] = '114.122.14.50'
    ydl_opts.update({
        'skip_download': True,
        'extract_flat': 'in_playlist',
        'ignore_no_formats_error': True
    })

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise HTTPException(status_code=404, detail="Informasi media tidak ditemukan.")

            entries = info.get('entries')
            first_entry = entries[0] if entries and len(entries) > 0 else info

            title = info.get('title') or first_entry.get('title') or "Video Media"
            uploader = first_entry.get('uploader') or first_entry.get('channel') or info.get('uploader') or "Unknown"
            duration = first_entry.get('duration') or info.get('duration')
            thumbnail = first_entry.get('thumbnail') or info.get('thumbnail') or ""
            
            is_image = False
            image_urls: List[str] = []
            media_items: List[Dict[str, Any]] = []
            
            # Instagram post / carousel detection
            if platform == "instagram":
                if entries and len(entries) > 1:
                    for idx, e in enumerate(entries):
                        e_fmts = e.get('formats') or []
                        is_v = e.get('vcodec') not in (None, 'none') or any(f.get('vcodec') not in (None, 'none') for f in e_fmts)
                        thumb = e.get('thumbnail')
                        if not thumb and e.get('thumbnails'):
                            thumb = e['thumbnails'][-1].get('url')
                        
                        v_url = e.get('url') or ""
                        if is_v and not v_url:
                            for f in reversed(e_fmts):
                                if f.get('vcodec') not in (None, 'none') and f.get('url'):
                                    v_url = f['url']
                                    break
                        
                        media_items.append({
                            "index": idx + 1,
                            "type": "video" if is_v else "image",
                            "url": v_url if is_v else (thumb or ""),
                            "thumbnail": thumb or "",
                            "width": e.get('width'),
                            "height": e.get('height')
                        })
                        if not is_v and thumb:
                            image_urls.append(thumb)
                    
                    has_video = any(m["type"] == "video" for m in media_items)
                    has_audio = has_video
                    is_image = all(m["type"] == "image" for m in media_items)
                else:
                    ext = first_entry.get('ext')
                    vcodec = first_entry.get('vcodec')
                    fmts = first_entry.get('formats') or []
                    has_video_fmt = any(f.get('vcodec') not in (None, 'none') for f in fmts)
                    if ext in ('jpg', 'jpeg', 'png', 'webp') or (not has_video_fmt and vcodec in (None, 'none')):
                        is_image = True
                        has_video = False
                        has_audio = False
                        if thumbnail:
                            image_urls.append(thumbnail)
                            media_items.append({
                                "index": 1,
                                "type": "image",
                                "url": thumbnail,
                                "thumbnail": thumbnail
                            })
                    else:
                        has_video = True
                        has_audio = True
                        is_image = False
                        media_items.append({
                            "index": 1,
                            "type": "video",
                            "url": first_entry.get('url') or "",
                            "thumbnail": thumbnail
                        })
            else:
                has_video = not is_image
                has_audio = not is_image
                if first_entry.get('thumbnails'):
                    thumbs = first_entry['thumbnails']
                    if len(thumbs) > 0 and not thumbnail:
                        thumbnail = thumbs[-1].get('url', '')

            # Calculate video qualities with estimated sizes in MB
            video_qualities = []
            seen_heights = set()
            dur = duration or 0
            
            formats = info.get('formats') or first_entry.get('formats') or []
            for f in sorted(formats, key=lambda x: (x.get('height') or 0, x.get('tbr') or 0), reverse=True):
                h = f.get('height')
                vcodec = f.get('vcodec')
                if h and vcodec not in (None, 'none') and h not in seen_heights and h >= 144:
                    seen_heights.add(h)
                    size_bytes = f.get('filesize') or f.get('filesize_approx')
                    if not size_bytes and f.get('tbr') and dur:
                        size_bytes = int((f['tbr'] * 1024 / 8) * dur)
                    size_mb = round(size_bytes / (1024 * 1024), 1) if size_bytes else None
                    
                    note = "Hemat"
                    if h >= 1080: note = "Full HD"
                    elif h >= 720: note = "HD"
                    elif h >= 480: note = "Standar"
                    
                    video_qualities.append({
                        "quality": str(h),
                        "resolution": f"{h}p",
                        "label": f"{h}p ({note})",
                        "size_mb": size_mb
                    })

            if has_video and not video_qualities:
                approx_mb = round(dur * 0.25, 1) if dur > 0 else None
                video_qualities.append({
                    "quality": "best",
                    "resolution": "Original (MP4)",
                    "label": "Original (Kualitas Penuh)",
                    "size_mb": approx_mb
                })

            # Calculate audio qualities with estimated sizes in MB
            audio_qualities = []
            if has_audio:
                audio_presets = [
                    {"bitrate": "320", "label": "320 kbps (Studio HQ)", "factor": 320},
                    {"bitrate": "192", "label": "192 kbps (Standar)", "factor": 192},
                    {"bitrate": "128", "label": "128 kbps (Ringan)", "factor": 128}
                ]
                for a in audio_presets:
                    size_mb = round((a["factor"] * 1024 / 8) * dur / (1024 * 1024), 1) if dur > 0 else None
                    audio_qualities.append({
                        "bitrate": a["bitrate"],
                        "label": a["label"],
                        "size_mb": size_mb
                    })

            return {
                "title": title,
                "uploader": uploader,
                "duration": format_duration(duration),
                "duration_seconds": duration,
                "thumbnail": thumbnail,
                "platform": platform,
                "has_video": has_video,
                "has_audio": has_audio,
                "is_image": is_image,
                "image_urls": image_urls,
                "media_items": media_items,
                "video_qualities": video_qualities,
                "audio_qualities": audio_qualities,
                "url": url,
            }

    except yt_dlp.utils.DownloadError as e:
        err_str = str(e)
        if "Private video" in err_str or "login" in err_str.lower():
            raise HTTPException(status_code=403, detail="Konten ini bersifat privat atau membutuhkan login.")
        if "Video unavailable" in err_str or "not found" in err_str.lower():
            raise HTTPException(status_code=404, detail="Media tidak ditemukan atau telah dihapus oleh pemiliknya.")
        raise HTTPException(status_code=400, detail=f"Gagal mengambil informasi media: {err_str[:120]}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Terjadi kesalahan saat memproses media: {str(e)}")

def extract_media_info(url: str, platform: str) -> Dict[str, Any]:
    """Router for media info extraction with in-memory TTL caching."""
    cached = get_cached_media_info(url, platform)
    if cached:
        return cached

    if platform == "threads":
        res = extract_threads_info(url)
    elif platform == "tiktok":
        res = extract_tiktok_info(url)
    else:
        res = extract_media_info_ytdlp(url, platform)

    set_cached_media_info(url, platform, res)
    return res

def download_media_file_ytdlp(url: str, format_type: str, platform: str, download_id: str, quality: Optional[str] = None, slide_index: Optional[int] = None) -> Dict[str, Any]:
    """
    Downloads media file (mp4, mp3, image, or zip) using yt-dlp.
    Supports resolution selection, bitrate selection, and carousel ZIP downloads.
    """
    # 1. Multi-media / Carousel ZIP handling (Instagram & others)
    if format_type == "zip" or (format_type == "image" and slide_index is None and platform == "instagram"):
        cached_info = get_cached_media_info(url, platform)
        media_items = (cached_info.get("media_items") if cached_info else None) or []
        title = (cached_info.get("title") if cached_info else None) or "instagram_media"
        
        if not media_items:
            ydl_opts = {'quiet': True, 'no_warnings': True, 'skip_download': True, 'ignore_no_formats_error': True}
            if platform == "instagram":
                ydl_opts['http_headers'] = {
                    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'X-Forwarded-For': '114.122.14.50',
                    'Client-IP': '114.122.14.50',
                }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                title = info.get('title') or "instagram_media"
                entries = info.get('entries')
                if entries and len(entries) > 1:
                    for i, e in enumerate(entries):
                        thumb = e.get('thumbnail') or (e.get('thumbnails') and e['thumbnails'][-1].get('url'))
                        is_v = e.get('vcodec') not in (None, 'none')
                        media_items.append({
                            "index": i + 1,
                            "type": "video" if is_v else "image",
                            "url": e.get('url') or thumb or "",
                            "thumbnail": thumb or ""
                        })

        if media_items and len(media_items) > 1 and format_type == "zip":
            headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.instagram.com/'}
            zip_path = create_zip_from_media_items(media_items, download_id, headers=headers)
            return {
                "file_path": zip_path,
                "title": f"{title}_all_media",
                "ext": "zip",
                "mime_type": "application/zip"
            }

    if format_type == "image":
        return download_image_directly(url, download_id, slide_index)
        
    out_template = str(TEMP_DIR / f"{download_id}.%(ext)s")
    ydl_opts = get_base_ydl_opts()
    if platform == "instagram":
        ydl_opts['http_headers']['User-Agent'] = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1'
        ydl_opts['http_headers']['Accept-Language'] = 'en-US,en;q=0.9'
        ydl_opts['http_headers']['X-Forwarded-For'] = '114.122.14.50'
        ydl_opts['http_headers']['Client-IP'] = '114.122.14.50'
    ydl_opts.update({
        'max_filesize': 300 * 1024 * 1024,
        'outtmpl': out_template,
    })
    if platform == "instagram":
        ydl_opts['ignore_no_formats_error'] = True
    
    # 2. Audio MP3 Format: Ensure audio-capable streams are selected across all platforms
    if format_type == "mp3":
        if platform == "youtube":
            fmt_selector = 'bestaudio/bestaudio*/18/best[acodec!=none]/best'
        elif platform == "instagram":
            fmt_selector = 'bestaudio/bestaudio*/best[acodec!=none]/1/2/3'
        else:
            fmt_selector = 'bestaudio/bestaudio*/best[acodec!=none]'
        ydl_opts.update({
            'format': fmt_selector,
        })
        expected_ext = "mp3"
        mime_type = "audio/mpeg"
        
    elif format_type == "mp4":
        if quality and quality.isdigit():
            if platform == "youtube":
                fmt_selector = f'bestvideo*[height<={quality}]+bestaudio/best[height<={quality}]/18/best'
            elif platform == "instagram":
                fmt_selector = f'bestvideo[height<={quality}]+bestaudio/best[height<={quality}][acodec!=none]/1/2/3/bestvideo+bestaudio'
            else:
                fmt_selector = f'bestvideo[height<={quality}]+bestaudio/best[height<={quality}][acodec!=none]/bestvideo+bestaudio'
        else:
            if platform == "youtube":
                fmt_selector = 'bestvideo*+bestaudio/bestvideo*+bestaudio*/18/best'
            elif platform == "instagram":
                fmt_selector = 'bestvideo+bestaudio/bestvideo*+bestaudio*/best[acodec!=none]/1/2/3'
            else:
                fmt_selector = 'bestvideo+bestaudio/bestvideo*+bestaudio*'
            
        ydl_opts.update({
            'format': fmt_selector,
            'merge_output_format': 'mp4',
        })
        expected_ext = "mp4"
        mime_type = "video/mp4"
    else:
        raise HTTPException(status_code=400, detail="Format yang diminta tidak didukung.")

    try:
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                title = info.get('title') or "media"
        except yt_dlp.utils.DownloadError as e:
            err_msg = str(e)
            logger.warning(f"Primary format download failed ({err_msg[:80]}), attempting fallback for {url}")
            
            downloaded = False
            title = "media"
            
            if 'cookiefile' in ydl_opts:
                try:
                    logger.info("Attempting Fallback 1: download without cookies...")
                    fb_no_cookie = dict(ydl_opts)
                    fb_no_cookie.pop('cookiefile', None)
                    if format_type == "mp3":
                        fb_no_cookie['format'] = 'bestaudio/bestaudio*/best'
                    else:
                        fb_no_cookie['format'] = 'bestvideo*+bestaudio/best'
                    fb_no_cookie['extractor_args'] = {
                        'youtube': {
                            'formats': ['missing_pot'],
                            'player_client': ['visionos', 'android']
                        }
                    }
                    with yt_dlp.YoutubeDL(fb_no_cookie) as ydl_nc:
                        info = ydl_nc.extract_info(url, download=True)
                        title = info.get('title') or "media"
                        downloaded = True
                except Exception as nc_err:
                    logger.warning(f"Fallback 1 failed: {nc_err}")
            
            if not downloaded:
                logger.info("Attempting Fallback 2: robust format fallback...")
                fallback_opts = dict(ydl_opts)
                fallback_opts.pop('cookiefile', None)
                if format_type == "mp3":
                    if platform == "youtube":
                        fallback_opts['format'] = 'bestaudio/bestaudio*/best'
                    elif platform == "instagram":
                        fallback_opts['format'] = 'bestaudio/bestaudio*/best[acodec!=none]/1/2/3'
                    else:
                        fallback_opts['format'] = 'bestaudio/best[acodec!=none]'
                else:
                    if platform == "youtube":
                        fallback_opts['format'] = 'bestvideo*+bestaudio/best'
                    elif platform == "instagram":
                        fallback_opts['format'] = 'bestvideo+bestaudio/best[acodec!=none]/1/2/3'
                    else:
                        fallback_opts['format'] = 'bestvideo+bestaudio/best'
                fallback_opts['extractor_args'] = {
                    'youtube': {
                        'formats': ['missing_pot'],
                        'player_client': ['visionos', 'android']
                    }
                }
                with yt_dlp.YoutubeDL(fallback_opts) as ydl_fb:
                    info = ydl_fb.extract_info(url, download=True)
                    title = info.get('title') or "media"

        # If MP3 was requested, perform direct FFmpeg audio extraction with quality bitrate
        if format_type == "mp3":
            mp3_file = TEMP_DIR / f"{download_id}.mp3"
            matching_files = [
                f for f in TEMP_DIR.glob(f"{download_id}.*")
                if f != mp3_file and not f.name.endswith(('.part', '.ytdl', '.temp'))
            ]
            # Prioritize pure audio stream containers (.m4a, .aac, .ogg, .opus, .mp3, .wav)
            matching_files.sort(key=lambda f: 0 if f.suffix.lower() in ('.m4a', '.aac', '.ogg', '.opus', '.mp3', '.wav') else 1)
            
            if matching_files:
                downloaded_file = matching_files[0]
                if downloaded_file.suffix.lower() != ".mp3":
                    convert_to_mp3_direct(downloaded_file, mp3_file, bitrate=quality or "320")
                    try:
                        downloaded_file.unlink()
                    except OSError:
                        pass
                else:
                    if downloaded_file != mp3_file:
                        downloaded_file.replace(mp3_file)
                        
            if mp3_file.exists():
                return {
                    "file_path": mp3_file,
                    "title": title,
                    "ext": "mp3",
                    "mime_type": mime_type
                }

        # Locate downloaded MP4 file
        expected_file = TEMP_DIR / f"{download_id}.{expected_ext}"
        if expected_file.exists():
            return {
                "file_path": expected_file,
                "title": title,
                "ext": expected_ext,
                "mime_type": mime_type
            }
            
        matching = list(TEMP_DIR.glob(f"{download_id}.*"))
        if matching:
            found_file = matching[0]
            ext = found_file.suffix.lstrip(".")
            return {
                "file_path": found_file,
                "title": title,
                "ext": ext,
                "mime_type": mime_type
            }
            
        raise HTTPException(status_code=500, detail="File media gagal disimpan ke penyimpanan sementara.")

    except HTTPException:
        raise
    except yt_dlp.utils.DownloadError as e:
        err_str = str(e)
        if "Requested format is not available" in err_str:
            raise HTTPException(status_code=400, detail=f"Format atau kualitas video yang diminta tidak tersedia: {err_str[:120]}")
        if "Private video" in err_str or "login" in err_str.lower():
            raise HTTPException(status_code=403, detail="Video ini bersifat privat atau dibatasi usia.")
        raise HTTPException(status_code=400, detail=f"Gagal mengunduh media: {err_str[:120]}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Kesalahan saat mengunduh: {str(e)}")

def download_media_file(url: str, format_type: str, platform: str, quality: Optional[str] = None, slide_index: Optional[int] = None) -> Dict[str, Any]:
    """Master router for media downloads."""
    download_id = uuid.uuid4().hex
    if platform == "threads":
        return download_threads_media(url, format_type, download_id, quality, slide_index)
    if platform == "tiktok":
        return download_tiktok_media(url, format_type, download_id, quality, slide_index)
    return download_media_file_ytdlp(url, format_type, platform, download_id, quality, slide_index)

def download_image_directly(url: str, download_id: str, slide_index: Optional[int] = None) -> Dict[str, Any]:
    """
    Downloads image for Instagram image/carousel posts.
    Uses cache or extracts specific slide index.
    """
    cached_info = get_cached_media_info(url, "instagram")
    media_items = (cached_info.get("media_items") if cached_info else None) or []
    image_urls = (cached_info.get("image_urls") if cached_info else None) or []
    title = (cached_info.get("title") if cached_info else None) or "instagram_image"

    target_idx = 0
    if slide_index is not None and slide_index > 0:
        target_idx = slide_index - 1
    else:
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        if 'img_index' in qs:
            try:
                target_idx = max(0, int(qs['img_index'][0]) - 1)
            except (ValueError, IndexError):
                target_idx = 0

    img_url = ""
    if media_items and target_idx < len(media_items):
        img_url = media_items[target_idx].get("thumbnail") or media_items[target_idx].get("url")
    elif image_urls and target_idx < len(image_urls):
        img_url = image_urls[target_idx]

    if not img_url:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'ignore_no_formats_error': True,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1',
                'Accept-Language': 'en-US,en;q=0.9',
                'X-Forwarded-For': '114.122.14.50',
                'Client-IP': '114.122.14.50',
            }
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get('title') or "instagram_image"
            entries = info.get('entries')
            if entries and len(entries) > 0:
                chosen = entries[target_idx] if target_idx < len(entries) else entries[0]
                img_url = chosen.get('thumbnail')
                if not img_url and chosen.get('thumbnails'):
                    img_url = chosen['thumbnails'][-1].get('url')
            else:
                img_url = info.get('url')
                if not img_url or not any(ext in img_url.lower() for ext in ('.jpg', '.jpeg', '.png', '.webp')):
                    img_url = info.get('thumbnail')
                if not img_url and info.get('thumbnails'):
                    img_url = info['thumbnails'][-1].get('url')
                
    if not img_url:
        raise HTTPException(status_code=404, detail="Tidak dapat mengekstrak tautan gambar dari postingan ini.")
        
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            'Referer': 'https://www.instagram.com/'
        }
        out_path = TEMP_DIR / f"{download_id}.jpg"
        stream_download_to_file(img_url, out_path, headers=headers)
        return {
            "file_path": out_path,
            "title": f"{title}_slide_{target_idx+1}" if slide_index else title,
            "ext": "jpg",
            "mime_type": "image/jpeg"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal mengunduh gambar: {str(e)}")
