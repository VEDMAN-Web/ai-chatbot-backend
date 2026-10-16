"""CORS: our sites and apps allowed, arbitrary origins refused."""
import re
from app.main import _CORS_ORIGIN_REGEX


def _ok(origin: str) -> bool:
    return re.fullmatch(_CORS_ORIGIN_REGEX, origin) is not None


def test_our_origins_allowed():
    for o in ("https://varsoviaestate.com", "https://www.varsoviaestate.com", "https://tajlandia.com",
              "https://tajlandia.pl", "https://thai.estate", "http://localhost:3000", "http://127.0.0.1:8000",
              "https://ai-chatbot-web-client.vercel.app",
              "https://ai-chatbot-admin-web-git-ai-chatbot-admin-dev-sbt-pm.vercel.app"):
        assert _ok(o), o


def test_foreign_origins_refused():
    for o in ("https://evil.example", "https://varsoviaestate.com.evil.example", "https://notvarsoviaestate.com",
              "https://other-app.vercel.app", "http://localhost.evil.example"):
        assert not _ok(o), o
