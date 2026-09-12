# Converter RB — Secure Media Downloader & MP4-to-MP3 Converter

Aplikasi web modern, ringan, berkinerja tinggi, dan dibangun dengan fokus utama pada **keamanan siber ketat (Anti-Injection & Anti-Cyber Attack)** untuk mengonversi dan mengunduh konten dari berbagai platform populer.

![License](https://img.shields.io/badge/license-MIT-blue)
![Python](https://img.shields.io/badge/python-3.12-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-green)
![Security](https://img.shields.io/badge/Security-Hardened-emerald)

---

## 🌟 Fitur Utama

- **Instagram**:
  - Download video Reels dan postingan video ke **MP4**.
  - Ekstraksi audio ke **MP3 (HQ 320 kbps)**.
  - Download **Gambar / Foto** resolusi tinggi asli (termasuk carousel).
- **Threads**:
  - Download video Threads ke **MP4**.
  - Ekstraksi audio Threads ke **MP3 (HQ 320 kbps)**.
- **YouTube & YouTube Shorts**:
  - Download video reguler & shorts ke **MP4** (resolusi optimal dengan muxing audio via FFmpeg).
  - Ekstraksi audio ke **MP3 (HQ 320 kbps)**.
- **Local Converter (MP4 ke MP3)**:
  - Drag & drop file MP4 lokal (hingga 150 MB).
  - Pilihan bitrate: `128 kbps` (ringan), `192 kbps` (optimal standar), `320 kbps` (studio quality).
  - Konversi instan berbasis FFmpeg engine dengan auto-download ke browser.
- **UI/UX Minimalis & Bersih (NO AI SLOP)**:
  - Tema dark slate elegan bergaya engineering modern (Linear / Vercel style).
  - Deteksi platform otomatis saat link ditempel (YouTube, Shorts, Instagram, Threads badge).
  - Pratinjau thumbnail, judul, nama kreator, dan durasi.
  - Tombol pintasan paste 1-klik dari clipboard.
  - Zero bloated libraries — waktu muat halaman secepat kilat (< 100ms).

---

## 🛡️ Proteksi Keamanan Siber (Security Hardening)

Aplikasi ini dirancang khusus untuk menangkal berbagai teknik eksploitasi dan injeksi:

1. **Anti-Command Injection (Zero Shell Execution)**:
   - Pemrosesan tautan menggunakan library Python native `yt_dlp.YoutubeDL` secara *in-process*, bukan perintah CLI shell.
   - Konversi FFmpeg menggunakan `subprocess.run(..., shell=False)` dengan array argumen statis dan path acak UUID. Tidak ada string interpolasi shell sama sekali.
2. **Anti-SSRF (Server-Side Request Forgery)**:
   - Whitelist ketat domain yang diizinkan (`youtube.com`, `youtu.be`, `instagram.com`, `threads.net`, `threads.com`).
   - Resolusi DNS aktif sebelum request: memblokir dan menolak akses ke semua rentang IP privat (`127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.0.0/16`, `::1`, link-local, multicast, dan cloud metadata).
3. **Anti-Path Traversal & Insecure Direct Object References**:
   - File sementara disimpan di folder terisolasi dengan nama acak `uuid.uuid4().hex`.
   - Nama file download disanitasi ketat dari karakter berbahaya (`..`, `/`, `\`, null byte `\x00`, karakter ilegal Windows `<>:"/\|?*`).
   - Format header `Content-Disposition` menggunakan standar RFC 5987 / RFC 6266.
4. **Anti-DoS & Resource Exhaustion**:
   - Batas ukuran upload MP4 sebesar 150 MB ditegakkan saat streaming.
   - In-memory rate limiting per alamat IP client (30 req/menit untuk info, 10 req/menit untuk unduh & konversi).
   - Penghapusan otomatis file temporer secara langsung melalui `BackgroundTask` setelah response dikirim.
5. **Anti-XSS & Security Headers**:
   - Tampilan antarmuka frontend menggunakan DOM API aman (`textContent` dan atribut langsung), tanpa penggunaan `innerHTML` pada data eksternal.
   - Middleware security headers aktif: CSP ketat, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `X-XSS-Protection: 1; mode=block`.

---

## 🚀 Cara Menjalankan

### Cara 1: Menggunakan Launcher Cepat (Windows)
Cukup klik ganda file:
```
run.bat
```
Script akan secara otomatis memeriksa environment, membuka browser di `http://localhost:8000`, dan menjalankan server backend FastAPI.

---

### Cara 2: Menjalankan Manual via Terminal

1. Buka PowerShell / Terminal di folder proyek:
   ```powershell
   cd "c:\Users\Z Series\MainRB\Code Project\Converter RB"
   ```

2. Jalankan server menggunakan virtual environment:
   ```powershell
   .\venv\Scripts\python.exe -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 8000
   ```

3. Buka browser Anda dan akses:
   ```
   http://localhost:8000
   ```

---

## 🧪 Menjalankan Uji Keamanan Otomatis

Untuk memverifikasi semua lapisan proteksi keamanan:
```powershell
.\venv\Scripts\python.exe -m unittest tests/test_security.py
```
Hasil uji:
- Validasi URL & Whitelist domain
- Deteksi & pemblokiran SSRF (localhost, 127.0.0.1, private IP, metadata cloud)
- Pembersihan path traversal & null byte injection
- Pengujian rate limiter per IP
