"""Vercel Python serverless 단일 진입점.

vercel.json 의 routes 로 /api/* 모두 이 파일이 받음.
backend/server.py 의 ApiHandler 가 path 별로 라우팅 처리.
"""
import sys
from pathlib import Path

# 프로젝트 루트를 PYTHONPATH 에 추가 (backend 모듈 import 용)
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.server import ApiHandler  # noqa: E402


# Vercel Python runtime 은 top-level 'handler' 클래스를 찾는다.
# `import as handler` 별칭은 인식 못 하므로 명시적 class 정의로 export.
class handler(ApiHandler):
    """Top-level handler — Vercel entrypoint."""
    pass
