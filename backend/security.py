import re
import socket
import ipaddress
from urllib.parse import urlparse, quote
import time
from collections import defaultdict
from typing import Tuple, Optional
from fastapi import HTTPException, Request

# Allowed domain patterns
ALLOWED_DOMAINS = {
    "youtube": [
        re.compile(r"^(?:[a-zA-Z0-9-]+\.)*youtube\.com$"),
        re.compile(r"^youtu\.be$")
    ],
    "instagram": [
        re.compile(r"^(?:[a-zA-Z0-9-]+\.)*instagram\.com$")
    ],
    "threads": [
        re.compile(r"^(?:[a-zA-Z0-9-]+\.)*threads\.net$"),
        re.compile(r"^(?:[a-zA-Z0-9-]+\.)*threads\.com$")
    ],
    "tiktok": [
        re.compile(r"^(?:[a-zA-Z0-9-]+\.)*tiktok\.com$"),
        re.compile(r"^vt\.tiktok\.com$"),
        re.compile(r"^vm\.tiktok\.com$")
    ]
}

# Forbidden IP networks (SSRF defense)
FORBIDDEN_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),       # Loopback
    ipaddress.ip_network("10.0.0.0/8"),        # Private
    ipaddress.ip_network("172.16.0.0/12"),     # Private
    ipaddress.ip_network("192.168.0.0/16"),    # Private
    ipaddress.ip_network("169.254.0.0/16"),    # Link-local / Cloud metadata
    ipaddress.ip_network("0.0.0.0/8"),         # Current network
    ipaddress.ip_network("224.0.0.0/4"),       # Multicast
    ipaddress.ip_network("240.0.0.0/4"),       # Reserved
    ipaddress.ip_network("::1/128"),           # IPv6 Loopback
    ipaddress.ip_network("fc00::/7"),          # IPv6 Private
    ipaddress.ip_network("fe80::/10"),         # IPv6 Link-local
]

def sanitize_filename(name: str, max_len: int = 100) -> str:
    """
    Sanitize a filename to prevent path traversal and filesystem injection.
    Removes dangerous characters and limits length.
    """
    if not name:
        return "download"
    
    # Strip path separators, null bytes, and Windows reserved characters
    cleaned = re.sub(r'[\x00-\x1f\\/:*?"<>|]', '_', name)
    cleaned = re.sub(r'\.\.+', '.', cleaned) # remove multiple consecutive dots
    cleaned = re.sub(r'^[\._\s]+', '', cleaned) # strip leading dots, underscores, spaces
    cleaned = re.sub(r'[\._\s]+$', '', cleaned) # strip trailing dots, underscores, spaces
    
    if not cleaned:
        cleaned = "download"
        
    return cleaned[:max_len]

def build_content_disposition(filename: str) -> str:
    """
    Safely construct Content-Disposition header with RFC 5987 / RFC 6266 encoding.
    Ensures legacy filename parameter contains strictly ASCII characters so Starlette's
    Latin-1 header encoder never raises UnicodeEncodeError on Unicode/foreign titles.
    """
    sanitized = sanitize_filename(filename)
    
    parts = sanitized.rsplit('.', 1)
    if len(parts) == 2:
        base, ext = parts[0], parts[1]
        ext_clean = re.sub(r'[^a-zA-Z0-9]', '', ext)
    else:
        base, ext_clean = sanitized, ""

    # Strictly ASCII (a-z, A-Z, 0-9, _, -)
    ascii_base = re.sub(r'[^a-zA-Z0-9_\-]', '_', base)
    ascii_base = re.sub(r'_+', '_', ascii_base).strip(' _-')
    if not ascii_base:
        ascii_base = "download"
    ascii_name = f"{ascii_base}.{ext_clean}" if ext_clean else ascii_base
    ascii_name = ascii_name.encode('ascii', 'ignore').decode('ascii')
    if not ascii_name:
        ascii_name = f"download.{ext_clean}" if ext_clean else "download"

    encoded_name = quote(sanitized, encoding='utf-8')
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}'

def validate_and_classify_url(raw_url: str) -> Tuple[str, str]:
    """
    Validates URL against SSRF, dangerous characters, and verifies domain whitelist.
    Returns (platform, cleaned_url).
    Raises HTTPException on validation failure.
    """
    if not raw_url or not isinstance(raw_url, str):
        raise HTTPException(status_code=400, detail="URL tidak valid atau kosong.")
    
    url = raw_url.strip()
    
    # Length guard
    if len(url) > 2048:
        raise HTTPException(status_code=400, detail="URL terlalu panjang (maksimum 2048 karakter).")
    
    # Prevent control chars / null byte injection
    if re.search(r'[\x00-\x1f]', url):
        raise HTTPException(status_code=400, detail="Karakter tidak diizinkan ditemukan dalam URL.")
    
    # Ensure scheme
    if not (url.startswith("http://") or url.startswith("https://")):
        url = "https://" + url
    
    try:
        parsed = urlparse(url)
    except Exception:
        raise HTTPException(status_code=400, detail="Format URL tidak dapat diproses.")
    
    # Only http and https
    if parsed.scheme.lower() not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Protokol URL harus HTTP atau HTTPS.")
    
    # Strip userinfo (user:pass@host)
    hostname = parsed.hostname
    if not hostname:
        raise HTTPException(status_code=400, detail="Hostname tidak valid.")
        
    hostname = hostname.lower()
    
    # Domain whitelist check
    matched_platform = None
    for platform, patterns in ALLOWED_DOMAINS.items():
        for pattern in patterns:
            if pattern.match(hostname):
                matched_platform = platform
                break
        if matched_platform:
            break
            
    if not matched_platform:
        raise HTTPException(
            status_code=400,
            detail="Domain tidak didukung. Layanan ini mendukung YouTube, Instagram, Threads, dan TikTok."
        )
    
    # Anti-SSRF: Resolve hostname to IP and ensure it is not private/loopback/metadata
    try:
        addr_info = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
        for family, _, _, _, sockaddr in addr_info:
            ip_str = sockaddr[0]
            ip_obj = ipaddress.ip_address(ip_str)
            for forbidden_net in FORBIDDEN_NETWORKS:
                if ip_obj in forbidden_net:
                    raise HTTPException(
                        status_code=403,
                        detail="Akses ke alamat IP privat/lokal diblokir demi keamanan."
                    )
    except HTTPException:
        raise
    except socket.gaierror:
        raise HTTPException(status_code=400, detail=f"Gagal menyelesaikan alamat server: {hostname}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Validasi keamanan URL gagal: {str(e)}")

    # Clean standard URL reconstruct
    cleaned_url = parsed._replace(netloc=parsed.netloc.split('@')[-1]).geturl()
    return matched_platform, cleaned_url


class InMemoryRateLimiter:
    """
    Lightweight, thread-safe in-memory rate limiter per IP address.
    """
    def __init__(self, limit: int, window_seconds: int = 60):
        self.limit = limit
        self.window_seconds = window_seconds
        self.requests = defaultdict(list)
        
    def check_rate_limit(self, client_ip: str):
        now = time.time()
        # Clean older records
        history = self.requests[client_ip]
        self.requests[client_ip] = [t for t in history if now - t < self.window_seconds]
        
        if len(self.requests[client_ip]) >= self.limit:
            raise HTTPException(
                status_code=429,
                detail="Terlalu banyak permintaan. Silakan tunggu sebentar sebelum mencoba kembali."
            )
        self.requests[client_ip].append(now)

# Global rate limiters
info_rate_limiter = InMemoryRateLimiter(limit=30, window_seconds=60)
download_rate_limiter = InMemoryRateLimiter(limit=10, window_seconds=60)
conversion_rate_limiter = InMemoryRateLimiter(limit=10, window_seconds=60)

def get_client_ip(request: Request) -> str:
    """
    Safely get client IP from request headers or client connection.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # Take the first IP
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "127.0.0.1"
