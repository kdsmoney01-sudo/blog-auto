"""외부 RSS 수집기 — 한국 IT/AI 트렌드 큐레이션용.

사용자 vault·trading·NAS 자료 0건 사용. 100% 외부 공개 RSS 만.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import feedparser
import requests
from bs4 import BeautifulSoup

log = logging.getLogger("rss")

# 한국어 AI/IT 큐레이션용 RSS 소스
SOURCES = [
    # 글로벌 IT/AI (영문, LLM 으로 한국어 가공)
    ("hn", "Hacker News (상위)", "https://hnrss.org/frontpage?points=100", "en"),
    ("hn_ai", "HN — AI 키워드", "https://hnrss.org/newest?q=AI+OR+LLM+OR+GPT&points=50", "en"),
    ("reddit_ml", "r/MachineLearning", "https://www.reddit.com/r/MachineLearning/.rss", "en"),
    ("reddit_localllama", "r/LocalLLaMA", "https://www.reddit.com/r/LocalLLaMA/.rss", "en"),
    ("devto", "dev.to AI", "https://dev.to/feed/tag/ai", "en"),
    ("devto_ml", "dev.to ML", "https://dev.to/feed/tag/machinelearning", "en"),
    # 한국어 직접 (가공 X)
    ("kr_zdnet", "ZDNet Korea IT", "https://feeds.feedburner.com/zdkorea", "ko"),
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) blog-auto/1.0 (+https://aikoreabrew.blogspot.com)",
    "Accept": "application/rss+xml, application/atom+xml, text/xml, */*",
}


@dataclass
class Article:
    source: str
    source_lang: str
    title: str
    url: str
    summary: str
    published: str
    fetched_at: str


def _clean_html(html: str, max_len: int = 800) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    return text[:max_len]


def _fetch_one(source_id: str, source_name: str, url: str, lang: str,
               max_items: int = 10) -> list[Article]:
    out: list[Article] = []
    try:
        # User-Agent 우회: requests 로 받아 feedparser 에 넘김
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        feed = feedparser.parse(r.content)
    except Exception as e:  # noqa: BLE001
        log.warning(f"  [{source_id}] fetch fail: {e}")
        return out

    for entry in feed.entries[:max_items]:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        summary = _clean_html(entry.get("summary", "") or entry.get("description", ""))
        published = entry.get("published") or entry.get("updated") or ""
        if not title or not link:
            continue
        out.append(Article(
            source=source_id,
            source_lang=lang,
            title=title,
            url=link,
            summary=summary,
            published=published,
            fetched_at=datetime.now(timezone.utc).isoformat(),
        ))
    return out


def collect_all(per_source: int = 10) -> list[Article]:
    articles: list[Article] = []
    for sid, sname, url, lang in SOURCES:
        items = _fetch_one(sid, sname, url, lang, per_source)
        log.info(f"  [{sid}] {sname}: {len(items)}개")
        articles.extend(items)
    return articles


def save_raw(articles: list[Article], out_dir: Path) -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    out_dir = out_dir / "raw" / today
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"rss_{datetime.now().strftime('%H%M%S')}.json"
    path.write_text(
        json.dumps([asdict(a) for a in articles], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info(f"raw 저장: {path} ({len(articles)} articles)")
    return path


def pick_top(articles: list[Article], n: int = 5, hours_window: int = 48) -> list[Article]:
    """최근 N시간 내 글 중 source 다양성 + 영어 우선 (한국어 가공 가치) N개."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_window)

    def _ts(a: Article) -> datetime:
        try:
            t = datetime(*feedparser._parse_date(a.published)[:6], tzinfo=timezone.utc)
            return t
        except Exception:
            return datetime.now(timezone.utc)

    recent = [a for a in articles if _ts(a) >= cutoff]
    recent.sort(key=_ts, reverse=True)

    # source 다양성 (한 source 최대 2개)
    out: list[Article] = []
    src_count: dict[str, int] = {}
    for a in recent:
        if src_count.get(a.source, 0) >= 2:
            continue
        out.append(a)
        src_count[a.source] = src_count.get(a.source, 0) + 1
        if len(out) >= n:
            break
    return out


if __name__ == "__main__":
    from common import load_env, setup_logging
    load_env()
    log = setup_logging("rss_test")
    log.info("RSS 수집 시작")
    articles = collect_all(per_source=5)
    log.info(f"총 {len(articles)}개")
    top = pick_top(articles, n=5)
    log.info(f"상위 5개 픽:")
    for a in top:
        log.info(f"  - [{a.source}] {a.title[:80]} ({a.url})")
