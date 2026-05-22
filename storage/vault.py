"""vault/블로그_수익/ 저장 — 글·이미지·메타 영구 보관.

사용자 vault 다른 도메인 (gmsv/주식/trading 등) 절대 X.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from collectors.rss import Article
from generators.article import BlogPost

log = logging.getLogger("vault")


def _slugify(text: str) -> str:
    """한글·영문 혼합 슬러그 (파일명 안전)."""
    s = re.sub(r"[\\/:*?\"<>|]", "", text)
    s = re.sub(r"\s+", "_", s.strip())
    return s[:60]


def vault_dir() -> Path:
    p = Path(os.environ.get("VAULT_DIR", r"C:\docker\obsidian\vault\블로그_수익"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def save_raw_rss(articles: list[Article]) -> Path:
    """수집한 원본 RSS 저장."""
    today = datetime.now().strftime("%Y-%m-%d")
    d = vault_dir() / "raw" / today
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"rss_{datetime.now().strftime('%H%M%S')}.json"
    p.write_text(
        json.dumps([{
            "source": a.source, "lang": a.source_lang, "title": a.title,
            "url": a.url, "summary": a.summary, "published": a.published,
            "fetched_at": a.fetched_at,
        } for a in articles], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info(f"raw 저장: {p}")
    return p


def save_post(post: BlogPost, blogger_post_id: str | None = None,
              blogger_url: str | None = None, status: str = "draft") -> Path:
    """생성한 글 마크다운 저장 (frontmatter + HTML)."""
    today = datetime.now().strftime("%Y-%m-%d")
    timestamp = datetime.now().strftime("%H%M%S")
    slug = _slugify(post.title)
    d = vault_dir() / ("posts" if status == "published" else "drafts") / today
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{timestamp}_{slug}.md"

    frontmatter = {
        "title": post.title,
        "type": "blog_post",
        "domain": "블로그_수익",
        "date": today,
        "time": datetime.now().strftime("%H:%M"),
        "status": status,
        "blogger_post_id": blogger_post_id,
        "blogger_url": blogger_url,
        "llm_backend": post.llm_backend,
        "llm_model": post.llm_model,
        "word_count": post.word_count,
        "labels": post.labels,
        "sources": post.sources_used,
        "tags": ["블로그", "수익", "자동발행"] + post.labels,
    }

    fm_lines = ["---"]
    for k, v in frontmatter.items():
        if isinstance(v, (list, dict)):
            fm_lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
        elif v is None:
            fm_lines.append(f"{k}:")
        else:
            fm_lines.append(f"{k}: {v}")
    fm_lines.append("---\n")

    body = f"# {post.title}\n\n{post.content_html}\n"
    p.write_text("\n".join(fm_lines) + body, encoding="utf-8")
    log.info(f"글 저장: {p} ({status})")
    return p


def save_analytics_entry(entry: dict) -> Path:
    """발행 기록 누적 (조회수·수익 추적 기반)."""
    p = vault_dir() / "analytics.json"
    data = []
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = []
    data.append({**entry, "logged_at": datetime.now().isoformat()})
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p
