"""Vercel Python serverless 단일 진입점.

vercel.json 의 routes 로 /api/* 모두 이 파일이 받음.
backend/server.py 의 ApiHandler 가 path 별로 라우팅 처리.
"""
import sys
import json
import traceback
from pathlib import Path
from http.server import BaseHTTPRequestHandler

# 프로젝트 루트를 PYTHONPATH 에 추가 (backend 모듈 import 용)
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_IMPORT_ERROR: str | None = None
try:
    from backend.server import ApiHandler
    _BASE = ApiHandler
except Exception:
    _IMPORT_ERROR = traceback.format_exc()
    _BASE = BaseHTTPRequestHandler


class handler(_BASE):  # type: ignore[valid-type]
    """Top-level handler — Vercel entrypoint."""

    def do_GET(self):
        if _IMPORT_ERROR:
            self._debug_error()
        else:
            super().do_GET()

    def do_POST(self):
        if _IMPORT_ERROR:
            self._debug_error()
        else:
            super().do_POST()

    def do_OPTIONS(self):
        if _IMPORT_ERROR:
            self._debug_error()
        else:
            super().do_OPTIONS()

    def _debug_error(self):
        body = json.dumps({"error": "import failed", "traceback": _IMPORT_ERROR},
                          ensure_ascii=False).encode("utf-8")
        self.send_response(500)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
