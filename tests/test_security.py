import unittest
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from security import (
    validate_and_classify_url,
    sanitize_filename,
    build_content_disposition,
    InMemoryRateLimiter
)
from fastapi import HTTPException

class TestSecurity(unittest.TestCase):

    def test_valid_youtube_urls(self):
        urls = [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://m.youtube.com/shorts/3fHqJqWpD9s",
            "https://youtube.com/shorts/3fHqJqWpD9s"
        ]
        for url in urls:
            platform, clean = validate_and_classify_url(url)
            self.assertEqual(platform, "youtube")
            self.assertTrue(clean.startswith("https://"))

    def test_valid_instagram_urls(self):
        urls = [
            "https://www.instagram.com/reel/C8xYz12345/",
            "https://instagram.com/p/C9abc12345/"
        ]
        for url in urls:
            platform, clean = validate_and_classify_url(url)
            self.assertEqual(platform, "instagram")

    def test_valid_threads_urls(self):
        urls = [
            "https://www.threads.net/@user/post/C8xYz12345",
            "https://threads.net/@user/post/C8xYz12345"
        ]
        for url in urls:
            platform, clean = validate_and_classify_url(url)
            self.assertEqual(platform, "threads")

    def test_unsupported_or_malicious_domains(self):
        evil_urls = [
            "https://evil-site.com/video.mp4",
            "https://youtube.com.attacker.com/watch?v=123",
            "http://127.0.0.1:8000/secret",
            "http://localhost:5000/api",
            "http://169.254.169.254/latest/meta-data/",
            "https://facebook.com/watch/?v=123",
            "javascript:alert(1)"
        ]
        for url in evil_urls:
            with self.assertRaises(HTTPException):
                validate_and_classify_url(url)

    def test_command_injection_and_null_bytes(self):
        malicious_inputs = [
            "https://youtube.com/watch?v=123;rm -rf /",
            "https://youtube.com/watch?v=123\x00&param=test",
            "https://youtube.com/watch?v=`calc.exe`",
            "https://youtube.com/watch?v=$(whoami)"
        ]
        # Any null bytes or control chars should be rejected
        with self.assertRaises(HTTPException):
            validate_and_classify_url("https://youtube.com/watch?v=123\x00")

    def test_filename_sanitization(self):
        cases = [
            ("../../etc/passwd", "etc_passwd"),
            ("..\\..\\Windows\\System32\\cmd.exe", "Windows_System32_cmd.exe"),
            ("My Video: Ep. 1 *Special*?.mp4", "My Video_ Ep. 1 _Special__.mp4"),
            ("Normal Title 123", "Normal Title 123"),
            ("", "download"),
            ("   ...   ", "download"),
            ("A" * 150, "A" * 100)  # Length limit
        ]
        for raw, expected in cases:
            cleaned = sanitize_filename(raw)
            self.assertEqual(cleaned, expected)

    def test_content_disposition_header(self):
        header = build_content_disposition("My Video (1080p).mp4")
        self.assertIn('attachment;', header)
        self.assertIn('filename=', header)
        self.assertIn("filename*=UTF-8''", header)

    def test_rate_limiter(self):
        limiter = InMemoryRateLimiter(limit=3, window_seconds=10)
        # First 3 should pass
        limiter.check_rate_limit("1.2.3.4")
        limiter.check_rate_limit("1.2.3.4")
        limiter.check_rate_limit("1.2.3.4")
        # 4th should raise 429
        with self.assertRaises(HTTPException) as ctx:
            limiter.check_rate_limit("1.2.3.4")
        self.assertEqual(ctx.exception.status_code, 429)
        # Different IP should still work
        limiter.check_rate_limit("5.6.7.8")

if __name__ == "__main__":
    unittest.main()
