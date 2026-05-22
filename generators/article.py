"""RSS 원본 → 한국어 블로그 글 생성.

콘텐츠 구조 (8명 에이전트 리서치 기반):
- 원본 출처 attribution + value-add 의무
- 30% 요약 + 50% Claude/LLM 분석/맥락 + 20% 독자 가이드
- AI disclosure footer 명시
- 800자+ (AdSense thin content 회피)
- Helpful Content 9 질문 통과
"""
from __future__ import annotations

import logging
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


SYSTEM_PROMPT_KR = """당신은 한국어 IT/AI 트렌드 블로거 "오늘의 AI" 의 큐레이션 편집자입니다.

# 사명
오늘 해외 IT/AI 커뮤니티 (HN, Reddit, dev.to 등) 의 인기 글 여러 개를 종합해서, **한국 독자가 30초 안에 핵심을 파악하고 깊이 있는 분석을 얻을 수 있는 블로그 글** 1편을 작성합니다.

# 핵심 원칙
1. **원본 그대로 복사 X** — 출처 명시 + 한국어로 재구성
2. **30%-50%-20% 비율** — 원본 요약 30%, 본인 분석/맥락 50%, 독자 다음 단계 20%
3. **AI disclosure footer 의무** — 글 마지막에 "AI 보조로 작성" 명시
4. **저작권 안전** — 핵심 사실·통계만 인용, 원문 문장 복사 X
5. **한국 독자 관점** — 글로벌 뉴스를 한국 시장·개발자 관점에서 풀이
6. **800자 이상** (Helpful Content 통과)

# 출력 형식 (마크다운)

## [구체적이고 검색되는 제목 — 25자 내외]

> **요약 (TL;DR)**: 1~2문장으로 오늘 글의 핵심을 미리 정리.

**📌 오늘의 트렌드**:
(3~5개 글의 공통점·맥락. 본문 2~3문단)

**🔍 깊이 분석**:
(가장 중요한 1~2건을 한국 개발자 관점에서. 2~3문단)

**💡 한국 시장에 의미**:
(한국 IT/AI 업계에 어떤 영향. 1~2문단)

**👉 독자 액션**:
(독자가 오늘·이번 주 할 만한 일 2~3개)

---

**참고 자료**:
- [원본 1 제목](URL)
- [원본 2 제목](URL)
- ...

---

*이 글은 외부 RSS 자료를 AI(Claude·Llama·Qwen) 보조로 작성하고 사람이 검토했습니다.*
"""


def build_user_prompt(articles: list[Article]) -> str:
    """RSS 원본 5개를 LLM 프롬프트로 정리."""
    today = datetime.now().strftime("%Y년 %m월 %d일")
    parts = [f"오늘은 {today} 입니다. 다음은 해외 IT/AI 커뮤니티 인기 글입니다.\n"]

    for i, a in enumerate(articles, 1):
        parts.append(f"### 글 {i}")
        parts.append(f"- 제목: {a.title}")
        parts.append(f"- 출처: {a.source}")
        parts.append(f"- URL: {a.url}")
        parts.append(f"- 요약: {a.summary[:500]}")
        parts.append("")

    parts.append("위 글들을 종합해서 한국어 블로그 글 1편을 작성하세요. "
                 "원문 그대로 복사 금지. 800자 이상. AI disclosure footer 필수.")
    return "\n".join(parts)


_INLINE_STYLE = """
<style>
.aikb-post { font-family: -apple-system, "Noto Sans KR", "Apple SD Gothic Neo", sans-serif; line-height: 1.75; color: #1f2937; max-width: 760px; margin: 0 auto; }
.aikb-post h2 { color: #0f172a; border-bottom: 2px solid #6366f1; padding-bottom: 6px; margin-top: 32px; }
.aikb-post h3 { color: #1e293b; margin-top: 24px; }
.aikb-post blockquote { background: #eef2ff; border-left: 4px solid #6366f1; padding: 14px 18px; margin: 18px 0; border-radius: 6px; }
.aikb-post blockquote p { margin: 0; color: #1e1b4b; }
.aikb-post strong { color: #4338ca; font-weight: 700; }
.aikb-post ul, .aikb-post ol { padding-left: 24px; }
.aikb-post li { margin: 6px 0; }
.aikb-post p { margin: 14px 0; }
.aikb-post a { color: #4f46e5; text-decoration: underline; text-decoration-thickness: 1px; text-underline-offset: 2px; }
.aikb-post a:hover { color: #312e81; }
.aikb-post hr { border: 0; border-top: 1px dashed #c7d2fe; margin: 28px 0; }
.aikb-post code { background: #f3f4f6; padding: 2px 6px; border-radius: 4px; font-family: 'JetBrains Mono', Consolas, monospace; font-size: 0.92em; color: #be185d; }
.aikb-post .aikb-tldr { background: linear-gradient(135deg, #fef3c7, #fde68a); border: 1px solid #f59e0b; padding: 16px 20px; border-radius: 10px; margin: 20px 0; }
.aikb-post .aikb-sources { background: #f8fafc; border: 1px solid #e2e8f0; padding: 14px 18px; border-radius: 8px; margin-top: 24px; font-size: 0.95em; }
.aikb-post .aikb-disclosure { font-size: 0.88em; color: #64748b; font-style: italic; border-top: 1px solid #e2e8f0; padding-top: 14px; margin-top: 24px; }
.aikb-cover { width: 100%; max-width: 760px; border-radius: 12px; margin: 16px 0 24px; box-shadow: 0 4px 14px rgba(0,0,0,0.08); }
</style>
"""


def markdown_to_html(md: str, cover_image_url: str | None = None) -> str:
    import markdown as md_lib
    body = md_lib.markdown(md, extensions=["fenced_code", "tables", "nl2br"])

    # blockquote 첫 개를 강조 TL;DR 박스로
    body = body.replace("<blockquote>", "<blockquote class=\"aikb-tldr\">", 1)

    # 참고 자료·AI disclosure 섹션 자동 강조
    body = body.replace("<p><strong>참고 자료</strong>", "<div class=\"aikb-sources\"><strong>참고 자료</strong>")
    if "aikb-sources" in body:
        # </ul> 다음에 닫는 div 박기 (참고 자료 섹션이 ul/리스트 형태)
        idx = body.find("aikb-sources")
        next_hr = body.find("<hr", idx)
        if next_hr > 0:
            body = body[:next_hr] + "</div>" + body[next_hr:]

    # AI disclosure 섹션
    body = body.replace("<p><em>", "<p class=\"aikb-disclosure\"><em>")

    cover_html = ""
    if cover_image_url:
        cover_html = f'<img class="aikb-cover" src="{cover_image_url}" alt="cover" />'

    return _INLINE_STYLE + f'<div class="aikb-post">{cover_html}{body}</div>'


def extract_title(md: str) -> str:
    """첫 # 헤더 추출. 없으면 첫 줄."""
    for line in md.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip().lstrip("[").rstrip("]")
        if line.startswith("## "):
            return line[3:].strip().lstrip("[").rstrip("]")
    # fallback
    first = md.strip().splitlines()[0] if md.strip() else "오늘의 AI 트렌드"
    return first[:60]


def generate_post(articles: list[Article], with_image: bool = True) -> Optional[BlogPost]:
    if not articles:
        log.error("articles 0개")
        return None

    user_prompt = build_user_prompt(articles[:5])
    log.info(f"LLM 호출 (articles {len(articles)}, prompt {len(user_prompt)} chars)")

    result = chat(SYSTEM_PROMPT_KR, user_prompt)
    if not result:
        return None

    md = result.text.strip()
    title = extract_title(md)

    # 첫 헤더는 Blogger 의 title 로 들어가니 본문에서 제거
    md_body = md
    if md_body.startswith("# ") or md_body.startswith("## "):
        md_body = "\n".join(md_body.splitlines()[1:]).lstrip()

    # 이미지 생성 (옵션, 로컬 ComfyUI 활용)
    cover_url = None
    if with_image:
        try:
            from generators.image import is_alive, generate, topic_to_prompt, upload_to_github_raw
            if is_alive():
                log.info("ComfyUI 이미지 생성 시작 (예상 ~95초)")
                prompt = topic_to_prompt(title)
                img_bytes = generate(prompt)
                if img_bytes:
                    from datetime import datetime
                    fname = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                    cover_url = upload_to_github_raw(img_bytes, fname)
                    if cover_url:
                        log.info(f"  cover 이미지 URL: {cover_url}")
            else:
                log.info("ComfyUI 미가동 → 이미지 SKIP (글만 발행)")
        except Exception as e:  # noqa: BLE001
            log.warning(f"이미지 생성 실패 (continue without): {e}")

    html = markdown_to_html(md_body, cover_image_url=cover_url)

    return BlogPost(
        title=title,
        content_html=html,
        labels=["AI", "트렌드", "큐레이션", "오늘의AI"],
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
    log.info(f"상위 {len(top)}개 → LLM 글 생성")

    post = generate_post(top)
    if post:
        log.info(f"\n=== 생성 글 ===")
        log.info(f"제목: {post.title}")
        log.info(f"백엔드: {post.llm_backend}/{post.llm_model}")
        log.info(f"길이: {post.word_count}자")
        log.info(f"라벨: {post.labels}")
        log.info(f"출처: {len(post.sources_used)}개")
        log.info(f"\nHTML 앞부분:\n{post.content_html[:800]}")
    else:
        log.error("글 생성 실패")
