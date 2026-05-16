from __future__ import annotations

import os
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent
SCRIPT_DIR = BACKEND_DIR / "scripts"

# Vercel 배포 환경은 /var/task 가 read-only → /tmp 사용
_ON_VERCEL = bool(os.environ.get("VERCEL"))
GENERATED_DIR = Path("/tmp/generated") if _ON_VERCEL else BACKEND_DIR / "generated"


def _load_dotenv() -> None:
    env_path = BACKEND_DIR / ".env"
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


def get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


GEMINI_API_KEY = get_env("GEMINI_API_KEY")
GEMINI_MODEL = get_env("GEMINI_MODEL", "gemini-3-pro-preview")
HOST = get_env("BACKEND_HOST", "127.0.0.1")
PORT = int(get_env("BACKEND_PORT", "8000"))

try:
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass
