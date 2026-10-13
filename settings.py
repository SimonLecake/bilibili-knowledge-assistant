"""Environment-based configuration; local credentials never belong in Git."""

import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=False)

XFYUN_APPID = os.getenv("XFYUN_APPID", "")
XFYUN_API_SECRET = os.getenv("XFYUN_API_SECRET", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_API_URL = os.getenv("LLM_API_URL", "https://api.moonshot.cn/v1/chat/completions")
LLM_MODEL = os.getenv("LLM_MODEL", "")
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")

KB_DIR = os.getenv("KB_DIR", str(BASE_DIR / "kb_data"))
CACHE_DIR = os.getenv("CACHE_DIR", str(BASE_DIR / "video_cache"))
COOKIES_FILE = str(BASE_DIR / "bilibili_cookies.json")
ENABLE_DOCUMENT_ROUTING = os.getenv("ENABLE_DOCUMENT_ROUTING", "false").lower() == "true"


def resolve_ffmpeg():
    """Prefer an explicit path, then PATH, then imageio-ffmpeg's bundled binary."""
    configured = os.getenv("FFMPEG_PATH", "").strip()
    if configured:
        return configured
    discovered = shutil.which("ffmpeg")
    if discovered:
        return discovered
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


FFMPEG_PATH = resolve_ffmpeg()
