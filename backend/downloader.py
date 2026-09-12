import os
import uuid
import glob
import asyncio
from pathlib import Path
from typing import Dict, Any, List, Optional
import requests
from fastapi import HTTPException
import shutil
import yt_dlp

TEMP_DIR = Path(__file__).resolve().parent.parent / "temp"
TEMP_DIR.mkdir(exist_ok=True)

NODE_BIN = shutil.which("node")
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
    # When cookies are provided, exclude tv_downgraded (which caused reload error)
    # and let yt-dlp use web/mweb clients to unlock all video and audio formats!
    # When no cookies are provided, fall back to visionos and android to bypass bot checks.
    if opts.get('cookiefile'):
        opts['extractor_args'] = {
            'youtube': {
                'player_client': ['-tv_downgraded']
            }
        }
    else:
        opts['extractor_args'] = {
            'youtube': {
                'player_client': ['visionos', 'android']
            }
        }

    if NODE_BIN:
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

def extract_media_info(url: str, platform: str) -> Dict[str, Any]:
    """
    Extracts metadata from URL without downloading the file.
    Runs in a worker thread.
    """
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
                
            # Handle playlist / multi-item (take first or summarize)
            entries = info.get('entries')
            if entries and len(entries) > 0:
                first_entry = entries[0]
            else:
                first_entry = info

            title = first_entry.get('title') or info.get('title') or "Media"
            uploader = first_entry.get('uploader') or first_entry.get('channel') or info.get('uploader') or "Unknown"
            duration = first_entry.get('duration') or info.get('duration')
            thumbnail = first_entry.get('thumbnail') or info.get('thumbnail') or ""
            
            # Instagram image detection
            is_image = False
            image_urls: List[str] = []
            
            # Check if this is an image or carousel in Instagram
            if platform == "instagram":
                # Check format or ext
                ext = first_entry.get('ext')
                vcodec = first_entry.get('vcodec')
                if ext in ('jpg', 'jpeg', 'png', 'webp') or vcodec == 'none':
                    is_image = True
                
                # Check for carousel entries
                if entries:
                    for e in entries:
                        thumb = e.get('thumbnail') or e.get('url')
                        if thumb:
                            image_urls.append(thumb)
                elif thumbnail:
                    image_urls.append(thumbnail)
            
            # If thumbnail is relative or missing, check thumbnails array
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

def download_media_file(url: str, format_type: str, platform: str) -> Dict[str, Any]:
    """
    Downloads media file (mp4, mp3, or image) to temporary storage.
    Returns dict with path, title, extension, and mime type.
    """
    download_id = uuid.uuid4().hex
    out_template = str(TEMP_DIR / f"{download_id}.%(ext)s")
    
    ydl_opts = get_base_ydl_opts()
    ydl_opts.update({
        'max_filesize': 150 * 1024 * 1024, # 150MB
        'outtmpl': out_template,
    })
    
    if format_type == "mp3":
        ydl_opts.update({
            'format': 'bestaudio/best*/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '320',
            }]
        })
        expected_ext = "mp3"
        mime_type = "audio/mpeg"
        
    elif format_type == "mp4":
        # Merge best video + best audio with fallback to best stream
        ydl_opts.update({
            'format': 'bestvideo*+bestaudio/bestvideo+bestaudio/best*',
            'merge_output_format': 'mp4',
        })
        expected_ext = "mp4"
        mime_type = "video/mp4"
        
    elif format_type == "image":
        # For Instagram image download
        # First check if we can get direct image URL or download thumbnail
        return download_image_directly(url, download_id)
        
    else:
        raise HTTPException(status_code=400, detail="Format yang diminta tidak didukung (harus mp3, mp4, atau image).")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get('title') or "media"
            
        # Locate the downloaded file
        expected_file = TEMP_DIR / f"{download_id}.{expected_ext}"
        if expected_file.exists():
            return {
                "file_path": expected_file,
                "title": title,
                "ext": expected_ext,
                "mime_type": mime_type
            }
            
        # Look for matching pattern if extension changed
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

def download_image_directly(url: str, download_id: str) -> Dict[str, Any]:
    """
    Downloads image for Instagram image/carousel posts.
    """
    # First extract info to get image URL
    ydl_opts = {'quiet': True, 'no_warnings': True, 'skip_download': True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        title = info.get('title') or "instagram_image"
        
        # Determine image URL
        img_url = None
        if info.get('url') and any(ext in info.get('url', '').lower() for ext in ('.jpg', '.jpeg', '.png', '.webp')):
            img_url = info['url']
        elif info.get('thumbnail'):
            img_url = info['thumbnail']
            
    if not img_url:
        raise HTTPException(status_code=404, detail="Tidak dapat mengekstrak tautan gambar dari postingan ini.")
        
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        resp = requests.get(img_url, headers=headers, timeout=20, stream=True)
        if resp.status_code != 200:
            raise HTTPException(status_code=400, detail="Gagal mengambil file gambar dari server penyedia.")
            
        out_path = TEMP_DIR / f"{download_id}.jpg"
        with open(out_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    
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
