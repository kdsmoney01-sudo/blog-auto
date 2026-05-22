"""공통 유틸 — env 로딩, 로깅, 텔레그램 알림."""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent
LOG_DIR = ROOT / "_logs"
LOG_DIR.mkdir(exist_ok=True)


def _parse_env_file(path: Path) -> dict:
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_env() -> dict:
    """`.env` + `.env.keys` + `trading/.env` 통합 로딩 (충돌 시 .env 우선)."""
    env = {}
    for p in [
        Path(r"Z:\docker\.env.keys"),
        Path(r"Z:\docker\trading\.env"),
        ROOT / ".env",
    ]:
        env.update(_parse_env_file(p))
    # OS env 덮어쓰기
    for k, v in env.items():
        os.environ.setdefault(k, v)
    return env


def setup_logging(name: str, level: int = logging.INFO):
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    log_file = LOG_DIR / f"{name}_{datetime.now().strftime('%Y-%m')}.log"
    handlers = [
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)
    return logging.getLogger(name)


_log = logging.getLogger("common")


def telegram_send(msg: str, channel: str = "news") -> bool:
    """trading 봇 재사용. 실패해도 graceful (시스템 정상 작동)."""
    import requests

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return False

    chat_id_map = {
        "news": os.environ.get("TELEGRAM_NEWS_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID"),
        "stock": os.environ.get("TELEGRAM_CHAT_ID"),
        "blog": os.environ.get("TELEGRAM_BLOG_CHAT_ID") or os.environ.get("TELEGRAM_NEWS_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID"),
    }
    chat_id = chat_id_map.get(channel) or chat_id_map.get("news")
    if not chat_id:
        return False

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "disable_web_page_preview": True},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:  # noqa: BLE001
        _log.warning(f"telegram fail: {e}")
        return False
