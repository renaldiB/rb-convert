import os
import re
import uuid
import glob
import asyncio
import subprocess
import urllib.parse
from html import unescape
from pathlib import Path
from typing import Dict, Any, List, Optional
import requests
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

def get_base_ydl_opts() -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 30,
        'remote_components': ['ejs:github'],
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
        }
    }
    
    # Check for cookies from env var or file if configured
    cookies_env = os.environ.get("YOUTUBE_COOKIES")
    if cookies_env and cookies_env.strip():
        env_cookie_path = TEMP_DIR / "env_cookies.txt"
        cleaned_cookies = cookies_env.replace('\\n', '\n').strip()
        if not env_cookie_path.exists() or env_cookie_path.read_text(encoding="utf-8", errors="ignore") != cleaned_cookies:
            env_cookie_path.write_text(cleaned_cookies, encoding="utf-8")
        opts['cookiefile'] = str(env_cookie_path)
    elif COOKIES_FILE.exists():
        opts['cookiefile'] = str(COOKIES_FILE)

    # For YouTube extractor:
    # 'formats': ['missing_pot'] prevents yt-dlp from dropping formats when PO Token is absent
    # 'player_client': ['web_embedded', 'mweb', 'android', 'ios'] provides complete format coverage
    opts['extractor_args'] = {
        'youtube': {
            'formats': ['missing_pot'],
            'player_client': ['web_embedded', 'mweb', 'android', 'ios']
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
    """Streams a remote media file directly to disk with size limit enforcement."""
    if not headers:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        resp = requests.get(download_url, headers=headers, stream=True, timeout=30)
        if resp.status_code != 200:
            raise HTTPException(status_code=400, detail=f"Gagal mengunduh file media dari penyedia (Status {resp.status_code}).")
        bytes_written = 0
        with open(target_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    bytes_written += len(chunk)
                    if bytes_written > max_size:
                        raise HTTPException(status_code=413, detail="File media melebihi batas ukuran maksimum 150MB.")
                    f.write(chunk)
    except HTTPException:
        if target_path.exists():
            try:
                target_path.unlink()
            except OSError:
                pass
        raise
    except Exception as e:
        if target_path.exists():
            try:
                target_path.unlink()
            except OSError:
                pass
        raise HTTPException(status_code=500, detail=f"Terjadi kesalahan saat mengunduh data: {str(e)}")

def convert_to_mp3_direct(input_path: Path, output_path: Path, bitrate: str = "320") -> None:
    """Direct FFmpeg MP3 conversion without ffprobe dependency."""
    ffmpeg_cmd = FFMPEG_BIN or "ffmpeg"
    cmd = [
        ffmpeg_cmd,
        "-y",
        "-i", str(input_path.resolve()),
        "-vn",
        "-c:a", "libmp3lame",
        "-b:a", f"{bitrate}k",
        str(output_path.resolve())
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        if res.returncode != 0:
            err_log = res.stderr.decode("utf-8", errors="ignore")
            logger.error(f"FFmpeg MP3 conversion failed: {err_log[:300]}")
            raise HTTPException(status_code=500, detail="Gagal mengonversi audio ke MP3.")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Proses konversi audio memakan waktu terlalu lama (timeout).")
    except Exception as e:
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(status_code=500, detail=f"Kesalahan konversi audio: {str(e)}")

# =========================================================================
# Threads Dedicated Handler (OpenGraph Meta Extraction)
# =========================================================================
def extract_og_tag(html: str, tag_name: str) -> Optional[str]:
    p1 = rf'<meta[^>]+(?:property|name)=["\']{re.escape(tag_name)}["\'][^>]+content=["\']([^"\']+)["\']'
    p2 = rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']{re.escape(tag_name)}["\']'
    m = re.search(p1, html, re.IGNORECASE) or re.search(p2, html, re.IGNORECASE)
    return unescape(m.group(1).strip()) if m else None

def extract_threads_info(url: str) -> Dict[str, Any]:
    headers = {
        'User-Agent': 'facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    try:
        resp = requests.get(url, headers=headers, allow_redirects=True, timeout=15)
        if resp.status_code != 200:
            raise HTTPException(status_code=400, detail=f"Gagal mengakses halaman Threads (Status {resp.status_code}).")
        html = resp.text
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Gagal menghubungi server Threads: {str(e)}")

    title = extract_og_tag(html, "og:title") or "Threads Post"
    image = extract_og_tag(html, "og:image") or extract_og_tag(html, "og:image:secure_url") or ""
    video = extract_og_tag(html, "og:video") or extract_og_tag(html, "og:video:secure_url") or ""

    uploader = "Threads User"
    user_match = re.search(r'@([a-zA-Z0-9._]+)', resp.url) or re.search(r'@([a-zA-Z0-9._]+)', title)
    if user_match:
        uploader = user_match.group(1)

    has_video = bool(video)
    has_audio = bool(video)
    is_image = not has_video and bool(image)
    image_urls = [image] if image else []

    return {
        "title": title,
        "uploader": uploader,
        "duration": "N/A",
        "duration_seconds": None,
        "thumbnail": image,
        "platform": "threads",
        "has_video": has_video,
        "has_audio": has_audio,
        "is_image": is_image,
        "image_urls": image_urls,
        "url": url,
        "video_url": video,
    }

def download_threads_media(url: str, format_type: str, download_id: str) -> Dict[str, Any]:
    info = extract_threads_info(url)
    title = info["title"]
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
        'Referer': 'https://www.threads.net/'
    }

    if format_type == "image":
        img_url = info.get("thumbnail") or (info["image_urls"][0] if info.get("image_urls") else None)
        if not img_url:
            raise HTTPException(status_code=404, detail="Gambar tidak ditemukan pada postingan Threads ini.")
        out_path = TEMP_DIR / f"{download_id}.jpg"
        stream_download_to_file(img_url, out_path, headers=headers)
        return {
            "file_path": out_path,
            "title": title,
            "ext": "jpg",
            "mime_type": "image/jpeg"
        }

    elif format_type in ("mp4", "mp3"):
        video_url = info.get("video_url")
        if not video_url:
            raise HTTPException(status_code=400, detail="Tidak ada video pada postingan Threads ini.")

        if format_type == "mp4":
            out_path = TEMP_DIR / f"{download_id}.mp4"
            stream_download_to_file(video_url, out_path, headers=headers)
            return {
                "file_path": out_path,
                "title": title,
                "ext": "mp4",
                "mime_type": "video/mp4"
            }
        else:
            raw_path = TEMP_DIR / f"raw_{download_id}.mp4"
            stream_download_to_file(video_url, raw_path, headers=headers)
            mp3_path = TEMP_DIR / f"{download_id}.mp3"
            convert_to_mp3_direct(raw_path, mp3_path)
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
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    try:
        resp = requests.post(api_url, data={"url": url, "hd": 1}, headers=headers, timeout=15)
        if resp.status_code == 200:
            res_json = resp.json()
            if res_json.get("code") == 0:
                d = res_json.get("data", {})
                title = d.get("title") or "TikTok Video"
                author_data = d.get("author", {})
                uploader = author_data.get("nickname") or author_data.get("unique_id") or "TikTok Creator"
                duration = d.get("duration")
                thumbnail = d.get("cover") or d.get("origin_cover") or ""
                has_video = bool(d.get("play") or d.get("wmplay"))
                has_audio = bool(d.get("music") or has_video)
                return {
                    "title": title,
                    "uploader": uploader,
                    "duration": format_duration(duration),
                    "duration_seconds": duration,
                    "thumbnail": thumbnail,
                    "platform": "tiktok",
                    "has_video": has_video,
                    "has_audio": has_audio,
                    "is_image": False,
                    "image_urls": [],
                    "url": url,
                    "play_url": d.get("play") or d.get("wmplay"),
                    "music_url": d.get("music")
                }
    except Exception as e:
        logger.warning(f"TikWM API extraction failed ({e}), falling back to yt-dlp")

    return extract_media_info_ytdlp(url, "tiktok")

def download_tiktok_media(url: str, format_type: str, download_id: str) -> Dict[str, Any]:
    api_url = "https://www.tikwm.com/api/"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    try:
        resp = requests.post(api_url, data={"url": url, "hd": 1}, headers=headers, timeout=15)
        if resp.status_code == 200:
            res_json = resp.json()
            if res_json.get("code") == 0:
                d = res_json.get("data", {})
                title = d.get("title") or "TikTok Video"
                if format_type == "mp4":
                    play_url = d.get("play") or d.get("wmplay")
                    if not play_url:
                        raise HTTPException(status_code=404, detail="Tautan video TikTok tanpa watermark tidak tersedia.")
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
                        convert_to_mp3_direct(raw_audio, mp3_path)
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
                        convert_to_mp3_direct(raw_vid, mp3_path)
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
        logger.warning(f"TikWM download failed ({e}), falling back to yt-dlp")

    return download_media_file_ytdlp(url, format_type, "tiktok", download_id)

# =========================================================================
# yt-dlp Core Extraction & Download
# =========================================================================
def extract_media_info_ytdlp(url: str, platform: str) -> Dict[str, Any]:
    ydl_opts = get_base_ydl_opts()
    ydl_opts.update({
        'skip_download': True,
        'extract_flat': False,
        'ignore_no_formats_error': True,
        'format': 'all/best/bestvideo*+bestaudio/best*',
    })
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise HTTPException(status_code=404, detail="Tidak dapat menemukan konten media pada tautan ini.")
                
            entries = info.get('entries')
            if entries and len(entries) > 0:
                first_entry = entries[0]
            else:
                first_entry = info

            title = first_entry.get('title') or info.get('title') or "Media"
            uploader = first_entry.get('uploader') or first_entry.get('channel') or info.get('uploader') or "Unknown"
            duration = first_entry.get('duration') or info.get('duration')
            thumbnail = first_entry.get('thumbnail') or info.get('thumbnail') or ""
            
            is_image = False
            image_urls: List[str] = []
            
            # Instagram post / carousel detection
            if platform == "instagram":
                has_video_stream = False
                if entries:
                    for e in entries:
                        if e.get('vcodec') not in (None, 'none'):
                            has_video_stream = True
                            break
                        fmts = e.get('formats') or []
                        if any(f.get('vcodec') not in (None, 'none') for f in fmts):
                            has_video_stream = True
                            break
                        thumb = e.get('thumbnail')
                        if not thumb and e.get('thumbnails'):
                            thumb = e['thumbnails'][0].get('url')
                        if thumb:
                            image_urls.append(thumb)
                    if not has_video_stream:
                        is_image = True
                else:
                    ext = first_entry.get('ext')
                    vcodec = first_entry.get('vcodec')
                    fmts = first_entry.get('formats') or []
                    has_video_fmt = any(f.get('vcodec') not in (None, 'none') for f in fmts)
                    if ext in ('jpg', 'jpeg', 'png', 'webp') or (not has_video_fmt and vcodec in (None, 'none')):
                        is_image = True
                        if thumbnail:
                            image_urls.append(thumbnail)
            
            # Fallback thumbnail resolution
            if not thumbnail and first_entry.get('thumbnails'):
                thumbs = first_entry['thumbnails']
                if len(thumbs) > 0:
                    thumbnail = thumbs[-1].get('url', '')

            has_video = not is_image
            has_audio = not is_image

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
                "url": url
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
    """Router for media info extraction."""
    if platform == "threads":
        return extract_threads_info(url)
    if platform == "tiktok":
        return extract_tiktok_info(url)
    return extract_media_info_ytdlp(url, platform)

def download_media_file_ytdlp(url: str, format_type: str, platform: str, download_id: str) -> Dict[str, Any]:
    """
    Downloads media file (mp4, mp3, or image) using yt-dlp.
    For MP3, avoids fragile ffprobe post-processing by downloading the best stream
    and running direct FFmpeg LAME conversion.
    """
    out_template = str(TEMP_DIR / f"{download_id}.%(ext)s")
    
    ydl_opts = get_base_ydl_opts()
    ydl_opts.update({
        'max_filesize': 150 * 1024 * 1024,
        'outtmpl': out_template,
        'ignore_no_formats_error': True
    })
    
    if format_type == "mp3":
        fmt_selector = 'bestaudio/bestaudio*/best/18' if platform == "youtube" else 'bestaudio*/best*'
        ydl_opts.update({
            'format': fmt_selector,
        })
        expected_ext = "mp3"
        mime_type = "audio/mpeg"
        
    elif format_type == "mp4":
        fmt_selector = 'bestvideo*+bestaudio/bestvideo*+bestaudio*/best/18' if platform == "youtube" else 'bestvideo*+bestaudio*/best*'
        ydl_opts.update({
            'format': fmt_selector,
            'merge_output_format': 'mp4',
        })
        expected_ext = "mp4"
        mime_type = "video/mp4"
        
    elif format_type == "image":
        return download_image_directly(url, download_id)
        
    else:
        raise HTTPException(status_code=400, detail="Format yang diminta tidak didukung (harus mp3, mp4, atau image).")

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
            
            # Fallback 1: download without cookies
            if 'cookiefile' in ydl_opts:
                try:
                    logger.info("Attempting Fallback 1: download without cookies...")
                    fb_no_cookie = dict(ydl_opts)
                    fb_no_cookie.pop('cookiefile', None)
                    fb_no_cookie['format'] = 'bestaudio*/best/18' if format_type == "mp3" else 'bestvideo*+bestaudio*/best/18'
                    with yt_dlp.YoutubeDL(fb_no_cookie) as ydl_nc:
                        info = ydl_nc.extract_info(url, download=True)
                        title = info.get('title') or "media"
                        downloaded = True
                except Exception as nc_err:
                    logger.warning(f"Fallback 1 failed: {nc_err}")
            
            # Fallback 2: direct format 18
            if not downloaded:
                logger.info("Attempting Fallback 2: direct format 18...")
                fallback_opts = dict(ydl_opts)
                fallback_opts['format'] = '18/best/b'
                fallback_opts['extractor_args'] = {
                    'youtube': {
                        'formats': ['missing_pot'],
                        'player_client': ['android', 'mweb']
                    }
                }
                with yt_dlp.YoutubeDL(fallback_opts) as ydl_fb:
                    info = ydl_fb.extract_info(url, download=True)
                    title = info.get('title') or "media"

        # If MP3 was requested, perform direct FFmpeg audio extraction from downloaded file
        if format_type == "mp3":
            mp3_file = TEMP_DIR / f"{download_id}.mp3"
            matching_files = [f for f in TEMP_DIR.glob(f"{download_id}.*") if f != mp3_file]
            
            if matching_files:
                downloaded_file = matching_files[0]
                if downloaded_file.suffix.lower() != ".mp3":
                    convert_to_mp3_direct(downloaded_file, mp3_file)
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
            
        raise HTTPException(status_code=500, detail="File hasil unduhan tidak ditemukan di server.")
        
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=f"Gagal mengunduh media: {str(e)[:150]}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Kesalahan saat mengunduh: {str(e)}")

def download_media_file(url: str, format_type: str, platform: str) -> Dict[str, Any]:
    """Router for media downloading."""
    download_id = uuid.uuid4().hex
    if platform == "threads":
        return download_threads_media(url, format_type, download_id)
    if platform == "tiktok":
        return download_tiktok_media(url, format_type, download_id)
    return download_media_file_ytdlp(url, format_type, platform, download_id)

def download_image_directly(url: str, download_id: str) -> Dict[str, Any]:
    """
    Downloads image for Instagram image/carousel posts.
    Handles specific slide extraction via img_index query parameter.
    """
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    target_idx = 0
    if 'img_index' in qs:
        try:
            target_idx = max(0, int(qs['img_index'][0]) - 1)
        except (ValueError, IndexError):
            target_idx = 0

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'ignore_no_formats_error': True
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        title = info.get('title') or "instagram_image"
        
        entries = info.get('entries')
        if entries and len(entries) > 0:
            chosen = entries[target_idx] if target_idx < len(entries) else entries[0]
            img_url = chosen.get('thumbnail')
            if not img_url and chosen.get('thumbnails'):
                img_url = chosen['thumbnails'][0].get('url')
        else:
            img_url = info.get('url')
            if not img_url or not any(ext in img_url.lower() for ext in ('.jpg', '.jpeg', '.png', '.webp')):
                img_url = info.get('thumbnail')
            if not img_url and info.get('thumbnails'):
                img_url = info['thumbnails'][0].get('url')
            
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
            "title": title,
            "ext": "jpg",
            "mime_type": "image/jpeg"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal menyimpan gambar: {str(e)}")
