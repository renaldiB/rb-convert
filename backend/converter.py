import os
import uuid
import shutil
import asyncio
import subprocess
from pathlib import Path
from typing import Tuple
from fastapi import HTTPException, UploadFile

TEMP_DIR = Path(__file__).resolve().parent.parent / "temp"
TEMP_DIR.mkdir(exist_ok=True)

# Valid bitrate options
ALLOWED_BITRATES = {"128", "192", "256", "320"}
MAX_FILE_SIZE = 150 * 1024 * 1024  # 150 MB

def is_valid_mp4_header(first_bytes: bytes) -> bool:
    """
    Checks if initial bytes contain standard MP4/ISO box header.
    Standard MP4 files contain 'ftyp' at offset 4..8.
    """
    if len(first_bytes) < 12:
        return False
    return b'ftyp' in first_bytes[:16]

async def save_uploaded_file(upload_file: UploadFile) -> Tuple[str, Path]:
    """
    Streams upload file into an isolated temporary file with UUID.
    Enforces maximum size limit and validates container header.
    """
    file_id = uuid.uuid4().hex
    temp_input_path = TEMP_DIR / f"input_{file_id}.mp4"
    
    bytes_written = 0
    header_checked = False
    
    try:
        with open(temp_input_path, "wb") as f:
            while chunk := await upload_file.read(1024 * 1024):  # 1MB chunk
                bytes_written += len(chunk)
                if bytes_written > MAX_FILE_SIZE:
                    raise HTTPException(
                        status_code=413,
                        detail="Ukuran file melebihi batas maksimum yang diizinkan (150 MB)."
                    )
                
                if not header_checked and len(chunk) >= 12:
                    if not is_valid_mp4_header(chunk):
                        raise HTTPException(
                            status_code=400,
                            detail="File yang diunggah bukan format video MP4 yang valid."
                        )
                    header_checked = True
                    
                f.write(chunk)
                
        if bytes_written == 0:
            raise HTTPException(status_code=400, detail="File kosong.")
            
    except Exception:
        # Clean up partial upload if failed
        if temp_input_path.exists():
            try:
                temp_input_path.unlink()
            except OSError:
                pass
        raise
        
    return file_id, temp_input_path

FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"

def convert_mp4_to_mp3(input_path: Path, file_id: str, bitrate: str = "320") -> Path:
    """
    Converts MP4 video to MP3 audio using FFmpeg with strict security:
    - shell=False
    - static parameter list
    - timeout enforcement
    - server-generated path
    """
    if bitrate not in ALLOWED_BITRATES:
        bitrate = "320"
        
    output_path = TEMP_DIR / f"output_{file_id}.mp3"
    
    cmd = [
        FFMPEG_BIN,
        "-y",
        "-i", str(input_path.resolve()),
        "-vn",                    # No video
        "-c:a", "libmp3lame",     # LAME MP3 encoder
        "-b:a", f"{bitrate}k",    # Bitrate
        str(output_path.resolve())
    ]
    
    try:
        result = subprocess.run(
            cmd,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=180
        )
        
        if result.returncode != 0:
            err_msg = result.stderr.decode('utf-8', errors='ignore')
            raise HTTPException(
                status_code=500,
                detail="Gagal mengonversi video. Pastikan file MP4 memiliki stream audio yang valid."
            )
            
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise HTTPException(
                status_code=500,
                detail="Output file konversi tidak ditemukan atau kosong."
            )
            
        return output_path
        
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail="Proses konversi memakan waktu terlalu lama (timeout 180s)."
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Terjadi kesalahan saat memproses konversi: {str(e)}"
        )

def cleanup_file(*paths: Path):
    """
    Safely delete temporary files after request is completed.
    """
    for path in paths:
        if path and isinstance(path, Path) and path.exists():
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
