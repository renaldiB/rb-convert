import os
import re
import uuid
import glob
import asyncio
import subprocess
import urllib.parse
import zipfile
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
    clean_url = unescape(download_url).replace('\\/', '/').replace('\\u0026', '&').replace('&amp;', '&').strip()
    try:
        resp = requests.get(clean_url, headers=headers, stream=True, timeout=30)
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
    """
    Direct FFmpeg MP3 conversion with multi-encoder fallbacks:
    1. Try libmp3lame (Standard high-quality)
    2. Try mp3 (Built-in)
    3. Try auto-selected encoder
    """
    ffmpeg_cmd = FFMPEG_BIN or "ffmpeg"
    valid_bitrate = bitrate if bitrate in ("128", "192", "256", "320") else "320"
    
    attempts = [
        [ffmpeg_cmd, "-y", "-i", str(input_path.resolve()), "-vn", "-c:a", "libmp3lame", "-b:a", f"{valid_bitrate}k", str(output_path.resolve())],
        [ffmpeg_cmd, "-y", "-i", str(input_path.resolve()), "-vn", "-c:a", "mp3", "-b:a", f"{valid_bitrate}k", str(output_path.resolve())],
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

    logger.error(f"FFmpeg MP3 conversion failed. Stderr tail: {last_stderr[-500:]}")
    raise HTTPException(status_code=500, detail="Gagal mengonversi audio ke MP3.")

def create_zip_from_urls(image_urls: List[str], download_id: str, headers: Optional[Dict[str, str]] = None) -> Path:
    """Downloads all given image URLs and bundles them into a ZIP archive."""
    zip_path = TEMP_DIR / f"{download_id}.zip"
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zip_f:
        for idx, img_url in enumerate(image_urls):
            temp_slide = TEMP_DIR / f"{download_id}_slide_{idx+1}.jpg"
            try:
                stream_download_to_file(img_url, temp_slide, headers=headers)
                zip_f.write(temp_slide, arcname=f"slide_{idx+1:02d}.jpg")
            finally:
                if temp_slide.exists():
                    try:
                        temp_slide.unlink()
                    except OSError:
                        pass
    return zip_path

# =========================================================================
# Threads Dedicated Handler (Deep Post Media Extraction)
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
    
    html = ""
    resp_url = url
    try:
        resp = requests.get(url, headers=browser_headers, allow_redirects=True, timeout=15)
        if resp.status_code == 200:
            html = resp.text
            resp_url = str(resp.url)
    except Exception as e:
        logger.warning(f"Browser request failed for Threads: {e}")

    # Fallback to crawler UA if initial request failed or was blocked
    if not html:
        crawler_headers = {'User-Agent': 'facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)'}
        try:
            resp = requests.get(url, headers=crawler_headers, allow_redirects=True, timeout=15)
            html = resp.text
            resp_url = str(resp.url)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Gagal mengakses halaman Threads: {str(e)}")

    title = extract_og_tag(html, "og:title") or "Threads Post"
    uploader = "Threads User"
    user_match = re.search(r'@([a-zA-Z0-9._]+)', resp_url) or re.search(r'@([a-zA-Z0-9._]+)', title)
    if user_match:
        uploader = user_match.group(1)

    # 1. Search for actual post photos in HTML (t51.82787-15 is post photo CDN)
    matches = re.findall(r'https:[^"\'\s<>\\]*(?:fbcdn\.net|cdninstagram\.com)[^"\'\s<>\\]*', html)
    by_media_id: Dict[str, List[str]] = {}
    for m in matches:
        clean = unescape(m).replace('\\/', '/').replace('\\u0026', '&').replace('&amp;', '&').strip()
        if 't51.82787-15' in clean:
            match_id = re.search(r'_(\d{15,20})_', clean)
            if match_id:
                mid = match_id.group(1)
                by_media_id.setdefault(mid, []).append(clean)

    real_images: List[str] = []
    for mid, urls in by_media_id.items():
        best = urls[0]
        for u in urls:
            if '1080' in u or '1440' in u:
                best = u
                break
        real_images.append(best)

    # 2. Search for videos (.mp4) in HTML
    video_matches = re.findall(r'https:[^"\'\s<>\\]*(?:fbcdn\.net|cdninstagram\.com)[^"\'\s<>\\]*\.mp4[^"\'\s<>\\]*', html)
    video = ""
    if video_matches:
        video = video_matches[0].replace('\\/', '/').replace('\\u0026', '&')
    else:
        og_vid = extract_og_tag(html, "og:video") or extract_og_tag(html, "og:video:secure_url")
        if og_vid:
            video = og_vid

    # Fallback to og:image only if no genuine photos were discovered
    if not real_images and not video:
        og_img = extract_og_tag(html, "og:image") or extract_og_tag(html, "og:image:secure_url")
        if og_img:
            real_images.append(og_img)

    has_video = bool(video)
    has_audio = bool(video)
    is_image = not has_video and len(real_images) > 0
    thumbnail = real_images[0] if real_images else (extract_og_tag(html, "og:image") or "")

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
        "image_urls": real_images,
        "video_qualities": video_qualities,
        "audio_qualities": audio_qualities,
        "url": url,
        "video_url": video,
    }

def download_threads_media(url: str, format_type: str, download_id: str, quality: Optional[str] = None, slide_index: Optional[int] = None) -> Dict[str, Any]:
    info = extract_threads_info(url)
    title = info["title"]
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
        'Referer': 'https://www.threads.net/'
    }

    # ZIP download for all images
    if format_type == "zip" or (format_type == "image" and slide_index is None and len(info.get("image_urls", [])) > 1):
        image_urls = info.get("image_urls", [])
        if not image_urls:
            raise HTTPException(status_code=404, detail="Tidak ada gambar yang ditemukan untuk diunduh.")
        zip_path = create_zip_from_urls(image_urls, download_id, headers=headers)
        return {
            "file_path": zip_path,
            "title": f"{title}_all_photos",
            "ext": "zip",
            "mime_type": "application/zip"
        }

    elif format_type == "image":
        image_urls = info.get("image_urls", [])
        if not image_urls:
            raise HTTPException(status_code=404, detail="Gambar tidak ditemukan pada postingan Threads ini.")
        
        target_idx = 0
        if slide_index is not None and slide_index > 0:
            target_idx = min(slide_index - 1, len(image_urls) - 1)
        
        img_url = image_urls[target_idx]
        out_path = TEMP_DIR / f"{download_id}.jpg"
        stream_download_to_file(img_url, out_path, headers=headers)
        return {
            "file_path": out_path,
            "title": f"{title}_slide_{target_idx+1}",
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
                
                # Approximate size in MB
                dur = duration or 0
                video_size_mb = round(d.get("size", 0) / (1024 * 1024), 1) if d.get("size") else (round(dur * 0.18, 1) if dur > 0 else None)
                
                video_qualities = []
                if has_video:
                    video_qualities.append({
                        "quality": "hd",
                        "resolution": "HD Tanpa Watermark",
                        "label": "HD Tanpa Watermark",
                        "size_mb": video_size_mb
                    })

                audio_qualities = []
                if has_audio:
                    for br, lbl in [("320", "320 kbps (Studio HQ)"), ("192", "192 kbps (Standar)"), ("128", "128 kbps (Ringan)")]:
                        mb = round((int(br) * 1000 / 8) * dur / (1024 * 1024), 1) if dur > 0 else None
                        audio_qualities.append({
                            "bitrate": br,
                            "label": lbl,
                            "size_mb": mb
                        })

                # Check if TikTok post is an image album / slide
                images = d.get("images") or []
                is_image = len(images) > 0 and not has_video

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
                    "video_qualities": video_qualities,
                    "audio_qualities": audio_qualities,
                    "url": url,
                    "play_url": d.get("play") or d.get("wmplay"),
                    "music_url": d.get("music")
                }
    except Exception as e:
        logger.warning(f"TikWM API extraction failed ({e}), falling back to yt-dlp")

    return extract_media_info_ytdlp(url, "tiktok")

def download_tiktok_media(url: str, format_type: str, download_id: str, quality: Optional[str] = None, slide_index: Optional[int] = None) -> Dict[str, Any]:
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
                
                # Check for image album
                images = d.get("images") or []
                if format_type == "zip" or (format_type == "image" and slide_index is None and len(images) > 1):
                    zip_path = create_zip_from_urls(images, download_id, headers=headers)
                    return {
                        "file_path": zip_path,
                        "title": f"{title}_photos",
                        "ext": "zip",
                        "mime_type": "application/zip"
                    }
                elif format_type == "image" and images:
                    idx = min(max(0, (slide_index or 1) - 1), len(images) - 1)
                    out_path = TEMP_DIR / f"{download_id}.jpg"
                    stream_download_to_file(images[idx], out_path, headers=headers)
                    return {
                        "file_path": out_path,
                        "title": f"{title}_slide_{idx+1}",
                        "ext": "jpg",
                        "mime_type": "image/jpeg"
                    }

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
        logger.warning(f"TikWM download failed ({e}), falling back to yt-dlp")

    return download_media_file_ytdlp(url, format_type, "tiktok", download_id, quality, slide_index)

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

            # If platform is Instagram reel or no multi-height formats found, provide default best option
            if has_video and not video_qualities:
                approx_mb = round(dur * 0.25, 1) if dur > 0 else None
                video_qualities.append({
                    "quality": "best",
                    "resolution": "Original MP4",
                    "label": "Original (Kualitas Penuh)",
                    "size_mb": approx_mb
                })

            # Calculate audio qualities with estimated sizes in MB
            audio_qualities = []
            if has_audio:
                for br, lbl in [("320", "320 kbps (Studio HQ)"), ("192", "192 kbps (Standar)"), ("128", "128 kbps (Ringan)")]:
                    mb = round((int(br) * 1000 / 8) * dur / (1024 * 1024), 1) if dur > 0 else None
                    audio_qualities.append({
                        "bitrate": br,
                        "label": lbl,
                        "size_mb": mb
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
                "video_qualities": video_qualities,
                "audio_qualities": audio_qualities,
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

def download_media_file_ytdlp(url: str, format_type: str, platform: str, download_id: str, quality: Optional[str] = None, slide_index: Optional[int] = None) -> Dict[str, Any]:
    """
    Downloads media file (mp4, mp3, image, or zip) using yt-dlp.
    Supports resolution selection, bitrate selection, and carousel ZIP downloads.
    """
    # Multi-image ZIP handling for Instagram
    if format_type == "zip" or (format_type == "image" and slide_index is None and platform == "instagram"):
        # Check if this is a carousel with multiple images
        ydl_opts = {'quiet': True, 'no_warnings': True, 'skip_download': True, 'ignore_no_formats_error': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get('title') or "instagram_photos"
            entries = info.get('entries')
            if entries and len(entries) > 1 and format_type == "zip":
                image_urls = []
                for e in entries:
                    thumb = e.get('thumbnail') or (e.get('thumbnails') and e['thumbnails'][0].get('url'))
                    if thumb:
                        image_urls.append(thumb)
                if image_urls:
                    headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.instagram.com/'}
                    zip_path = create_zip_from_urls(image_urls, download_id, headers=headers)
                    return {
                        "file_path": zip_path,
                        "title": f"{title}_all_photos",
                        "ext": "zip",
                        "mime_type": "application/zip"
                    }

    if format_type == "image":
        return download_image_directly(url, download_id, slide_index)
        
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
        if quality and quality.isdigit():
            fmt_selector = f'bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best'
        else:
            fmt_selector = 'bestvideo*+bestaudio/bestvideo*+bestaudio*/best/18' if platform == "youtube" else 'bestvideo*+bestaudio*/best*'
            
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
                    fb_no_cookie['format'] = 'bestaudio*/best/18' if format_type == "mp3" else 'bestvideo*+bestaudio*/best/18'
                    with yt_dlp.YoutubeDL(fb_no_cookie) as ydl_nc:
                        info = ydl_nc.extract_info(url, download=True)
                        title = info.get('title') or "media"
                        downloaded = True
                except Exception as nc_err:
                    logger.warning(f"Fallback 1 failed: {nc_err}")
            
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

        # If MP3 was requested, perform direct FFmpeg audio extraction with quality bitrate
        if format_type == "mp3":
            mp3_file = TEMP_DIR / f"{download_id}.mp3"
            matching_files = [f for f in TEMP_DIR.glob(f"{download_id}.*") if f != mp3_file]
            
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
            
        raise HTTPException(status_code=500, detail="File hasil unduhan tidak ditemukan di server.")
        
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=f"Gagal mengunduh media: {str(e)[:150]}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Kesalahan saat mengunduh: {str(e)}")

def download_media_file(url: str, format_type: str, platform: str, quality: Optional[str] = None, slide_index: Optional[int] = None) -> Dict[str, Any]:
    """Router for media downloading."""
    download_id = uuid.uuid4().hex
    if platform == "threads":
        return download_threads_media(url, format_type, download_id, quality, slide_index)
    if platform == "tiktok":
        return download_tiktok_media(url, format_type, download_id, quality, slide_index)
    return download_media_file_ytdlp(url, format_type, platform, download_id, quality, slide_index)

def download_image_directly(url: str, download_id: str, slide_index: Optional[int] = None) -> Dict[str, Any]:
    """
    Downloads image for Instagram image/carousel posts.
    Handles specific slide extraction via slide_index or img_index query parameter.
    """
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    target_idx = 0
    if slide_index is not None and slide_index > 0:
        target_idx = slide_index - 1
    elif 'img_index' in qs:
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
            "title": f"{title}_slide_{target_idx+1}",
            "ext": "jpg",
            "mime_type": "image/jpeg"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal menyimpan gambar: {str(e)}")
