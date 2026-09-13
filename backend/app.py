import os
import time
import asyncio
import logging
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional

import requests
from fastapi import FastAPI, Request, Response, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.background import BackgroundTasks
from pydantic import BaseModel, Field

from security import (
    validate_and_classify_url,
    sanitize_filename,
    build_content_disposition,
    get_client_ip,
    info_rate_limiter,
    download_rate_limiter,
    conversion_rate_limiter,
)
from downloader import extract_media_info, download_media_file
from converter import save_uploaded_file, convert_mp4_to_mp3, cleanup_file

logger = logging.getLogger("app")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
TEMP_DIR = Path(__file__).resolve().parent.parent / "temp"
TEMP_DIR.mkdir(exist_ok=True)

APP_START_TIME = time.time()

# -------------------------------------------------------------------------
# Keep-Alive Background Worker for Render (Prevents Spin-Down/Sleep)
# -------------------------------------------------------------------------
async def keep_alive_worker():
    """
    Periodically sends an HTTP GET request to self every 10 minutes (600s).
    This resets Render's 15-minute idle inactivity timer, keeping the web service awake 24/7.
    """
    ping_interval = 600  # 10 minutes
    # Wait 60 seconds after startup before the first ping
    await asyncio.sleep(60)
    while True:
        render_url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("SELF_PING_URL")
        if render_url:
            target = f"{render_url.rstrip('/')}/api/health"
            try:
                res = await asyncio.to_thread(requests.get, target, timeout=15)
                logger.info(f"Keep-alive self-ping sent to {target} (Status: {res.status_code})")
            except Exception as e:
                logger.warning(f"Keep-alive self-ping to {target} failed: {e}")
        await asyncio.sleep(ping_interval)

@asynccontextmanager
async def lifespan(app: FastAPI):
    keep_alive_task = asyncio.create_task(keep_alive_worker())
    logger.info("Application startup complete. Keep-alive background worker active.")
    try:
        yield
    finally:
        keep_alive_task.cancel()
        logger.info("Keep-alive background worker stopped.")

app = FastAPI(
    title="Secure Media Downloader & Converter",
    description="Clean, lightweight, secure converter for Instagram, Threads, YouTube, and local MP4-to-MP3.",
    version="1.0.0",
    lifespan=lifespan
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # local app
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# Security Headers Middleware
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: https: blob:; "
        "media-src 'self' blob:; "
        "connect-src 'self';"
    )
    return response

# Pydantic Schemas
class MediaInfoRequest(BaseModel):
    url: str = Field(..., max_length=2048, description="URL tautan video/postingan")

class DownloadRequest(BaseModel):
    url: str = Field(..., max_length=2048, description="URL tautan video/postingan")
    format: str = Field(..., pattern="^(mp3|mp4|image|zip)$", description="Format output: mp3, mp4, image, atau zip")
    quality: Optional[str] = Field(None, max_length=50, description="Kualitas video/audio e.g. 1080, 720, 320, 192")
    slide_index: Optional[int] = Field(None, description="Nomor slide gambar/video yang ingin diunduh (1-based)")

@app.post("/api/info")
async def get_info(req: MediaInfoRequest, request: Request):
    client_ip = get_client_ip(request)
    info_rate_limiter.check_rate_limit(client_ip)
    
    # URL & SSRF Validation
    platform, clean_url = validate_and_classify_url(req.url)
    
    # Run info extraction in thread pool to prevent blocking event loop
    info = await asyncio.to_thread(extract_media_info, clean_url, platform)
    return JSONResponse(content={"success": True, "data": info})

@app.post("/api/download")
async def download_media(req: DownloadRequest, request: Request, background_tasks: BackgroundTasks):
    client_ip = get_client_ip(request)
    download_rate_limiter.check_rate_limit(client_ip)
    
    # URL & SSRF Validation
    platform, clean_url = validate_and_classify_url(req.url)
    
    # Download media file in thread pool
    media = await asyncio.to_thread(
        download_media_file,
        clean_url,
        req.format,
        platform,
        req.quality,
        req.slide_index
    )
    
    file_path = media["file_path"]
    filename = f"{sanitize_filename(media['title'])}.{media['ext']}"
    
    # Register background task for secure cleanup
    background_tasks.add_task(cleanup_file, file_path)
    
    headers = {
        "Content-Disposition": build_content_disposition(filename),
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0"
    }
    
    return FileResponse(
        path=str(file_path),
        media_type=media["mime_type"],
        headers=headers
    )

@app.post("/api/convert-file")
async def convert_file(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    bitrate: str = Form("320")
):
    client_ip = get_client_ip(request)
    conversion_rate_limiter.check_rate_limit(client_ip)
    
    # 1. Save uploaded file safely with UUID and size enforcement
    file_id, input_path = await save_uploaded_file(file)
    
    # 2. Convert MP4 to MP3 via safe FFmpeg
    try:
        output_path = await asyncio.to_thread(convert_mp4_to_mp3, input_path, file_id, bitrate)
    except Exception:
        # If conversion fails, ensure input file is deleted
        cleanup_file(input_path)
        raise
        
    original_stem = Path(file.filename or "audio").stem
    clean_stem = sanitize_filename(original_stem)
    filename = f"{clean_stem}.mp3"
    
    # 3. Schedule cleanup after response finishes streaming
    background_tasks.add_task(cleanup_file, input_path, output_path)
    
    headers = {
        "Content-Disposition": build_content_disposition(filename),
        "Cache-Control": "no-cache, no-store, must-revalidate"
    }
    
    return FileResponse(
        path=str(output_path),
        media_type="audio/mpeg",
        headers=headers
    )

@app.get("/api/health")
@app.get("/api/ping")
async def health_check():
    uptime = int(time.time() - APP_START_TIME)
    render_url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("SELF_PING_URL")
    return {
        "status": "healthy",
        "service": "Converter RB",
        "uptime_seconds": uptime,
        "render_url": render_url,
        "keep_alive_active": bool(render_url)
    }

@app.get("/api/debug-download")
async def debug_download():
    import subprocess
    import yt_dlp
    from downloader import get_base_ydl_opts, TEMP_DIR
    import uuid
    
    url = "https://www.instagram.com/reel/DdJUBLnveAu/?stkn=MTF3NmxwNG5leDhlaA=="
    test_id = f"dbg_{uuid.uuid4().hex[:8]}"
    out_template = str(TEMP_DIR / f"{test_id}.%(ext)s")
    
    ydl_opts = get_base_ydl_opts()
    ydl_opts['http_headers']['User-Agent'] = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1'
    ydl_opts['http_headers']['Accept-Language'] = 'en-US,en;q=0.9'
    ydl_opts.update({
        'format': 'bestaudio/bestaudio*/best[acodec!=none]/1/2/3',
        'outtmpl': out_template,
        'ignore_no_formats_error': True
    })
    
    report = {}
    try:
        import re, json
        from yt_dlp.utils import traverse_obj, urlencode_postdata
        ydl_raw = yt_dlp.YoutubeDL({'quiet': True})
        ie = ydl_raw.get_info_extractor('Instagram')
        video_id = 'DdJUBLnveAu'
        media_id = str(yt_dlp.extractor.instagram._id_to_pk(video_id))
        api_check = ie._download_json(
            f'{ie._API_BASE_URL}/web/get_ruling_for_content/', video_id,
            errnote=False, fatal=False, query={'content_type': 'MEDIA', 'target_id': media_id}) or {}
        csrf_token = ie._get_cookies('https://www.instagram.com').get('csrftoken')
        csrf = csrf_token.value if csrf_token and api_check.get('status') == 'ok' else None

        # Test 1: GraphQL with X-Forwarded-For (Indonesian residential IP)
        resp1 = ie._download_json(
            'https://www.instagram.com/api/graphql', video_id,
            fatal=False, impersonate=True,
            headers={
                **ie._api_headers,
                'X-FB-Friendly-Name': 'PolarisLoggedOutDesktopWWWPostRootContentQuery',
                'X-CSRFToken': csrf,
                'X-FB-LSD': ie._lsd_token,
                'X-Requested-With': 'XMLHttpRequest',
                'Referer': f'https://www.instagram.com/reel/{video_id}/',
                'X-Forwarded-For': '114.122.14.50',
                'Client-IP': '114.122.14.50',
            }, data=urlencode_postdata({
                'lsd': ie._lsd_token,
                'fb_api_caller_class': 'RelayModern',
                'fb_api_req_friendly_name': 'PolarisLoggedOutDesktopWWWPostRootContentQuery',
                'server_timestamps': 'true',
                'variables': json.dumps({'media_id': media_id}),
                'doc_id': '27130156389949648',
            }))
        prod1 = traverse_obj(resp1, ('data', 'xig_polaris_media', 'if_not_gated_logged_out', {dict})) or {}
        man1 = prod1.get('video_dash_manifest', '')
        report["exp1_has_audio_rep"] = bool(re.search(r'<Representation\b[^>]*codecs="mp4a[^"]*"', man1))
        report["exp1_reps"] = re.findall(r'<Representation\b[^>]*>', man1)

        # Test 2: Direct __a=1&__d=dis with mobile headers
        r_dis = requests.get(f'https://www.instagram.com/reel/{video_id}/?__a=1&__d=dis', headers={
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1',
            'Accept': '*/*',
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': f'https://www.instagram.com/reel/{video_id}/',
        }, timeout=10)
        report["exp2_status"] = r_dis.status_code
        if r_dis.status_code == 200:
            try:
                j = r_dis.json()
                report["exp2_keys"] = list(j.keys())
                report["exp2_has_audio"] = 'audio' in r_dis.text
            except Exception as je:
                report["exp2_json_err"] = str(je)

        # Test 3: Can Render fetch the audio CDN URL that we discovered locally?
        known_audio_url = "https://instagram.fcgk30-1.fna.fbcdn.net/o1/v/t2/f2/m78/AQM8A3SnaTTsLar7mFLCH5k51svobMn0J41kQQAvnFjAehV5neTWUYDZVwA-jK7uqCTnZwMgdSZS2XA8aSLcJPRfwSOr9Ss_B2pd2j0.mp4?_nc_cat=105&_nc_oc=AdpMyVKNU1NhuGWTmqVjjixmzilJlGDLyplVtNpugiqlEEAA7L_Db4eB8F-fKotJ-rY&_nc_sid=9ca052&_nc_ht=instagram.fcgk30-1.fna.fbcdn.net&_nc_ohc=7XQ2RAkWALgQ7kNvwE1jfLs&efg=eyJ2ZW5jb2RlX3RhZyI6ImlnLXhwdmRzLmNsaXBzLmlnd3d3LUMzLmRhc2hfbG5faGVhYWNfdmJyM19hdWRpbyIsInZpZGVvX2lkIjpudWxsLCJvaWxfdXJsZ2VuX2FwcF9pZCI6OTM2NjE5NzQzMzkyNDU5LCJjbGllbnRfbmFtZSI6ImlnIiwieHB2X2Fzc2V0X2lkIjoxMTA1MzA4ODk4ODI0MTQ3LCJhc3NldF9hZ2VfZGF5cyI6MiwidmlfdXNlY2FzZV9pZCI6MTAwOTksImR1cmF0aW9uX3MiOjIwLCJiaXRyYXRlIjo2NzUzMywidXJsZ2VuX3NvdXJjZSI6Ind3dyJ9&ccb=17-1&_nc_gid=ShbGAPp65eaDkWT9G1bEeQ&_nc_ss=7b689&_nc_zt=28&oh=00_AQLVflgwhkirDV5j1O6H9H-FAqNw3qFOluSxH9qF5FSIaQ&oe=6AA8798A"
        r_cdn = requests.head(known_audio_url, timeout=10)
        report["exp3_cdn_head_status"] = r_cdn.status_code
        report["exp3_cdn_content_length"] = r_cdn.headers.get("Content-Length")
    except Exception as raw_e:
        report["raw_graphql_error"] = str(raw_e)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            report["title"] = info.get("title")
            report["format_id"] = info.get("format_id")
            report["ext"] = info.get("ext")
            report["acodec"] = info.get("acodec")
            report["vcodec"] = info.get("vcodec")
            report["formats"] = [
                {"id": f.get("format_id"), "vcodec": f.get("vcodec"), "acodec": f.get("acodec"), "ext": f.get("ext")}
                for f in info.get("formats", [])
            ]
    except Exception as e:
        report["download_error"] = str(e)
        
    downloaded_files = list(TEMP_DIR.glob(f"{test_id}.*"))
    report["downloaded_files"] = [str(f.name) for f in downloaded_files]
    
    if downloaded_files:
        df = downloaded_files[0]
        report["file_size"] = df.stat().st_size
        try:
            p = subprocess.run(["ffprobe", "-v", "error", "-show_streams", str(df)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
            report["ffprobe_stdout"] = p.stdout
            report["ffprobe_stderr"] = p.stderr
        except Exception as pe:
            report["ffprobe_error"] = str(pe)
            
        out_mp3 = TEMP_DIR / f"{test_id}.mp3"
        try:
            c = subprocess.run(["ffmpeg", "-y", "-i", str(df), "-map", "0:a:0?", "-vn", "-c:a", "libmp3lame", "-b:a", "320k", str(out_mp3)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
            report["ffmpeg_returncode"] = c.returncode
            report["ffmpeg_stderr"] = c.stderr[-800:] if c.stderr else ""
            report["mp3_exists"] = out_mp3.exists()
            report["mp3_size"] = out_mp3.stat().st_size if out_mp3.exists() else 0
        except Exception as ce:
            report["ffmpeg_error"] = str(ce)
            
        for f in list(TEMP_DIR.glob(f"{test_id}.*")):
            try: f.unlink()
            except: pass
            
    return report

# Mount frontend files
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/favicon.ico")
    async def serve_favicon():
        favicon_file = FRONTEND_DIR / "favicon.svg"
        if favicon_file.exists():
            return FileResponse(str(favicon_file), media_type="image/svg+xml")
        return JSONResponse({"message": "Not found"}, status_code=404)

    @app.get("/")
    async def serve_index():
        index_file = FRONTEND_DIR / "index.html"
        if index_file.exists():
            return FileResponse(str(index_file))
        return JSONResponse({"message": "Frontend not found"}, status_code=404)
