@echo off
title Converter RB - Secure Media Converter
color 0b

echo ===================================================
echo             CONVERTER RB - LAUNCHER
echo    Clean, Minimalist, Fast ^& Secure Downloader
echo ===================================================
echo.

cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo [!] Virtual environment tidak ditemukan!
    echo [*] Sedang membuat venv...
    python -m venv venv
    call venv\Scripts\pip.exe install -r backend\requirements.txt
)

echo [*] Membuka browser di http://localhost:8000 ...
start http://localhost:8000

echo [*] Menjalankan server backend FastAPI...
echo [*] Tekan CTRL+C di jendela ini untuk menghentikan server.
echo.

venv\Scripts\python.exe -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 8000 --log-level info
pause
