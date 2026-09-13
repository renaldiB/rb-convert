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
        "keep_alive_active": bool(render_url),
        "has_cookies": bool(os.environ.get("YOUTUBE_COOKIES"))
    }

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
