"""RSS 원본 -> 한국어 블로그 글 생성.

설계 원칙 (2026-05-23 사용자 명시 "사람 글처럼"):
- AI disclosure footer 제거
- 정형 구조 강제 X (## 요약 / ## 분석 같은 패턴 없음)
- 1인칭 톤 + 구어체 + 매번 다른 길이/톤
- labels 동적 추출 (모든 글에 "AI", "트렌드" 같은 자동 라벨 X)
"""
from __future__ import annotations

import logging
import random
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from collectors.rss import Article
from generators.llm import chat

log = logging.getLogger("article")


@dataclass
class BlogPost:
    title: str
    content_html: str
    labels: list[str]
    sources_used: list[dict]  # [{title, url, source}]
    llm_backend: str
    llm_model: str
    word_count: int


# 사람 블로거 페르소나 — 1인칭 + 구어체 + 정형 구조 X
SYSTEM_PROMPT_KR = """당신은 "오늘의 AI" 블로그를 운영하는 30대 한국 IT 종사자입니다. 본업은 IT 회사 개발자이고, AI 트렌드에 관심이 많아 매일 해외 IT 커뮤니티 글을 읽고 본인 시각으로 풀어 적습니다.

# 글쓰기 톤 (사람 글)
- 1인칭으로 자연스럽게 ("저는", "제가", "솔직히 말하면", "근데", "사실")
- 친구한테 IT 트렌드 풀어주듯 구어체 섞기 (단 비속어 X)
- 정형 구조 (## 요약 / ## 분석 / ## 한국 시장 / ## 액션) 강제 X — 글마다 흐름이 다르게
- 부제목 (## ###) 최소화. 정말 흐름 끊을 때만 1-2개.
- 같은 문구 반복 금지 — "결론적으로", "따라서", "또한", "이는" 같은 정형 connective 피하기
- 본인 경험·의견 자연스럽게 ("제가 작년에 비슷한 일 있었는데..." 같은 짧은 일화 OK)
- 문장 길이 다양화 — 짧은 문장 사이사이에 긴 문장, 호흡 자연스럽게
- 가끔 질문 던지기 ("왜 이렇게 됐을까?", "이게 진짜 의미가 뭘까?")

# 출력 형식
- 첫 줄 = `## ` 한 헤더로 글 제목 (20-30자, 검색 잘 잡히는 자연스러운 한국어 제목, 클릭베이트 X)
- 본문 = 자유 흐름. 정형 구조 X. 마크다운 부제목 최소화.
- 출처 인용 = 본문 안에서 자연스럽게 (예: "HN 에 올라온 이야기인데...", "[dev.to 글](URL)에서 본 건데...") + 글 끝에 1-2 줄 더 자세한 출처
- AI disclosure / "AI 보조 작성" 같은 footer 박지 마세요
- 마무리 = 자연스럽게 끝내기. "결론적으로", "따라서" 금지. 본인 한 마디 또는 독자에게 가벼운 질문 던지기.

# 저작권 / 사실
- 원문 그대로 복사 절대 X. 본인 말로 재구성.
- 핵심 사실·통계만 인용. URL 본문 안에 자연스럽게 1-3개 박기.
- 한국 IT 시장·개발자 관점에서 풀이.

# 길이
- {LENGTH_INSTRUCTION}
"""


# 글 길이 다양화 (사람 블로거 = 매번 다른 길이)
_LENGTH_VARIANTS = [
    "1500자 정도. 짧고 핵심만, 한 주제 위주로 깊이.",
    "2500자 정도. 보통 길이, 2-3 주제 종합.",
    "3500자 정도. 깊이 있게 한 주제 분석.",
    "2000자 정도. 가볍게 트렌드 정리.",
    "3000자 정도. 깊이 + 본인 의견 비중 큼.",
    "1800자 정도. 짧지만 임팩트 있게.",
    "2200자 정도. 보통.",
]


# 글 톤 다양화 (사람 블로거 = 매일 다른 기분)
_TONE_HINTS = [
    "오늘은 비판적 시각으로 풀어주세요. 회의적인 관점 명확히.",
    "오늘은 흥분된 톤. 재밌는 발견 위주.",
    "오늘은 차분히 정리하는 톤. 깊이 있게.",
    "오늘은 본인 경험 사례 1-2개 자연스럽게 섞어주세요.",
    "오늘은 질문 던지면서 풀어주세요 (\"왜 그럴까?\", \"이게 가능할까?\").",
    "오늘은 약간 캐주얼하게. 친구한테 말하듯.",
    "오늘은 진지하게. 깊이 있는 분석 중심.",
]


def build_user_prompt(articles: list[Article]) -> tuple[str, str, str]:
    """RSS 원본 -> LLM prompt. 길이/톤 매번 다름. 시드는 날짜+첫 글 URL hash."""
    # 결정적 랜덤 (같은 RSS = 같은 결과, 단 매일 다름)
    seed_str = datetime.now().strftime("%Y%m%d") + (articles[0].url if articles else "")
    rnd = random.Random(seed_str)
    length_pick = rnd.choice(_LENGTH_VARIANTS)
    tone_pick = rnd.choice(_TONE_HINTS)

    today = datetime.now().strftime("%Y년 %m월 %d일")
    parts = [
        f"오늘 ({today}) 본 해외 IT/AI 커뮤니티 인기 글 {len(articles)}개입니다.",
        "",
        f"# 톤: {tone_pick}",
        "",
    ]

    for i, a in enumerate(articles, 1):
        parts.append(f"### 글 {i}")
        parts.append(f"- 제목: {a.title}")
        parts.append(f"- 출처: {a.source}")
        parts.append(f"- URL: {a.url}")
        parts.append(f"- 요약: {a.summary[:500]}")
        parts.append("")

    parts.append("위 글들 중 본인이 재밌거나 의미 있다 싶은 것 위주로 본인 시각으로 풀어주세요. "
                 "사람이 쓴 일반 블로그 글 같이. 정형 구조 강제 X. AI disclosure 박지 마세요.")
    return "\n".join(parts), length_pick, tone_pick


# 사람 블로그 스타일 CSS (화려한 그라디언트 줄임)
_INLINE_STYLE = """
<style>
.aikb-post { font-family: -apple-system, "Noto Sans KR", "Apple SD Gothic Neo", sans-serif; line-height: 1.8; color: #2d3748; max-width: 760px; margin: 0 auto; font-size: 16.5px; }
.aikb-post h2 { color: #1a202c; margin-top: 36px; margin-bottom: 14px; font-size: 1.45em; }
.aikb-post h3 { color: #2d3748; margin-top: 28px; font-size: 1.2em; }
.aikb-post p { margin: 16px 0; }
.aikb-post strong { color: #2c5282; font-weight: 600; }
.aikb-post ul, .aikb-post ol { padding-left: 24px; margin: 14px 0; }
.aikb-post li { margin: 8px 0; }
.aikb-post a { color: #2b6cb0; text-decoration: underline; text-decoration-thickness: 1px; text-underline-offset: 3px; }
.aikb-post a:hover { color: #2a4365; }
.aikb-post blockquote { border-left: 3px solid #cbd5e0; padding-left: 16px; margin: 18px 0; color: #4a5568; font-style: italic; }
.aikb-post code { background: #edf2f7; padding: 2px 6px; border-radius: 3px; font-family: Consolas, Monaco, monospace; font-size: 0.93em; color: #d53f8c; }
.aikb-post hr { border: 0; border-top: 1px solid #e2e8f0; margin: 32px 0; }
.aikb-cover { width: 100%; max-width: 760px; border-radius: 8px; margin: 12px 0 24px; }
</style>
"""


def markdown_to_html(md: str, cover_image_url: str | None = None) -> str:
    import markdown as md_lib
    body = md_lib.markdown(md, extensions=["fenced_code", "tables", "nl2br"])

    cover_html = ""
    if cover_image_url:
        cover_html = f'<img class="aikb-cover" src="{cover_image_url}" alt="" />'

    return _INLINE_STYLE + f'<div class="aikb-post">{cover_html}{body}</div>'


def extract_title(md: str) -> str:
    """첫 # 또는 ## 헤더 추출. 없으면 첫 줄."""
    for line in md.splitlines():
        line = line.strip()
        if line.startswith("## "):
            return line[3:].strip().lstrip("[").rstrip("]")
        if line.startswith("# "):
            return line[2:].strip().lstrip("[").rstrip("]")
    first = md.strip().splitlines()[0] if md.strip() else "오늘의 트렌드"
    return first[:60]


# label 동적 추출 (모든 글에 같은 라벨 박힘 회피)
_STOPWORDS = {
    "오늘", "내일", "어제", "그리고", "그래서", "하지만", "있는", "되는", "한다", "있다",
    "이런", "저런", "그런", "않는", "라는", "라고", "보다", "이상", "정도", "위해",
    "통해", "처럼", "같은", "다른", "여러", "많은", "모든", "어떤", "무엇", "관련",
    "사용", "활용", "이용", "기반", "이번", "지난", "최근", "현재", "과거", "미래",
    "AI", "트렌드",
}


def _extract_labels(title: str, content: str, max_n: int = 5) -> list[str]:
    text = title + " " + content[:800]
    kr = re.findall(r"[가-힣]{2,6}", text)
    en = re.findall(r"\b[A-Z][a-zA-Z]{1,14}\b", text)
    candidates = [w for w in kr + en if w not in _STOPWORDS]
    counts = Counter(candidates)
    return [w for w, _ in counts.most_common(max_n)]


def generate_post(articles: list[Article], with_image: bool = True) -> Optional[BlogPost]:
    if not articles:
        log.error("articles 0개")
        return None

    user_prompt, length_pick, tone_pick = build_user_prompt(articles[:5])
    sys_prompt = SYSTEM_PROMPT_KR.format(LENGTH_INSTRUCTION=length_pick)

    log.info(f"LLM 호출 (articles {len(articles)}, prompt {len(user_prompt)} chars)")
    log.info(f"  tone: {tone_pick[:40]}")
    log.info(f"  length: {length_pick[:40]}")

    result = chat(sys_prompt, user_prompt)
    if not result:
        return None

    md = result.text.strip()
    title = extract_title(md)

    md_body = md
    if md_body.startswith("# ") or md_body.startswith("## "):
        md_body = "\n".join(md_body.splitlines()[1:]).lstrip()

    # AI disclosure 자동 제거 (LLM 가끔 박음)
    disclosure_patterns = [
        r"\*이 글은.*AI.*작성.*\*",
        r"이 글은.*AI.*보조로.*작성.*",
        r"\*Note:.*AI.*\*",
        r"^.*AI.*보조.*작성.*검토.*$",
    ]
    for pat in disclosure_patterns:
        md_body = re.sub(pat, "", md_body, flags=re.MULTILINE | re.IGNORECASE)
    md_body = re.sub(r"\n{3,}", "\n\n", md_body).strip()

    cover_url = None
    if with_image:
        try:
            from generators.image import is_alive, generate, topic_to_prompt, upload_to_github_raw
            if is_alive():
                log.info("ComfyUI 이미지 생성 시작 (예상 ~95-120초)")
                prompt = topic_to_prompt(title)
                img_bytes = generate(prompt)
                if img_bytes:
                    fname = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                    cover_url = upload_to_github_raw(img_bytes, fname)
                    if cover_url:
                        log.info(f"  cover 이미지 URL: {cover_url}")
            else:
                log.info("ComfyUI 미가동 -> 이미지 SKIP")
        except Exception as e:  # noqa: BLE001
            log.warning(f"이미지 생성 실패 (continue without): {e}")

    html = markdown_to_html(md_body, cover_image_url=cover_url)

    labels = _extract_labels(title, md_body)
    if not labels:
        labels = ["블로그"]

    return BlogPost(
        title=title,
        content_html=html,
        labels=labels,
        sources_used=[{"title": a.title, "url": a.url, "source": a.source}
                      for a in articles[:5]],
        llm_backend=result.backend,
        llm_model=result.model,
        word_count=len(md_body),
    )


if __name__ == "__main__":
    from common import load_env, setup_logging
    from collectors.rss import collect_all, pick_top

    load_env()
    log = setup_logging("article_test")
    log.info("RSS 수집...")
    articles = collect_all(per_source=5)
    top = pick_top(articles, n=5)
    log.info(f"상위 {len(top)}개 -> LLM 글 생성")

    post = generate_post(top)
    if post:
        log.info("\n=== 생성 글 ===")
        log.info(f"제목: {post.title}")
        log.info(f"백엔드: {post.llm_backend}/{post.llm_model}")
        log.info(f"길이: {post.word_count}자")
        log.info(f"라벨: {post.labels}")
        log.info(f"출처: {len(post.sources_used)}개")
        log.info(f"\nHTML 앞부분:\n{post.content_html[:800]}")
    else:
        log.error("글 생성 실패")
