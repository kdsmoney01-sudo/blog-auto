"""품질 게이트 — Helpful Content 9 자체 평가 + 휴리스틱.

Google "Scaled Content Abuse" 페널티 회피 핵심:
- 글 자체 자동 평가 → 점수 낮으면 draft 보관
- 출처·길이·구조 휴리스틱 + LLM 자가 평가
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

from generators.article import BlogPost
from generators.llm import chat

log = logging.getLogger("quality_gate")


@dataclass
class GateResult:
    passed: bool
    score: int  # 0-9
    notes: list[str]
    risk_flags: list[str]


HELPFUL_CONTENT_QUESTIONS = """
다음 9가지 기준으로 평가하세요. 각 기준에 대해 "Y/N" + 한 줄 이유:

1. **Original**: 원본 정보·분석·인사이트가 있는가? (단순 출처 복사 X)
2. **Substantial**: 주제를 충분히 다루는가? (얕은 글 X)
3. **Insightful**: 명백한 사실 너머 통찰이 있는가?
4. **Avoid Copy**: 원문 그대로 복사·재배열만 X?
5. **Byline OK**: 저자/출처 명시되어 있는가?
6. **AI Disclosure**: AI 사용 명시되어 있는가?
7. **Useful**: 방문자가 직접 와서 봐도 유용한가?
8. **Trust**: 신뢰 가능한가? (사실 기반·과장 X)
9. **Purpose Clear**: "왜 이 글을 만들었나" 답할 수 있는가?

답 형식:
1Y/N | 이유
2Y/N | 이유
...
9Y/N | 이유
점수: N/9
"""


def _heuristic_check(post: BlogPost) -> tuple[int, list[str], list[str]]:
    """LLM 호출 전 빠른 휴리스틱 체크."""
    notes = []
    risks = []
    score = 9  # 시작 점수, 위반 시 깎음

    # 1. 길이 (800자+ AdSense thin content 회피)
    if post.word_count < 800:
        score -= 3
        risks.append(f"⚠ word_count {post.word_count} < 800")

    # 2. 출처 명시 (참고 자료 섹션 또는 URL 5개+)
    html = post.content_html
    url_count = len(re.findall(r"https?://", html))
    if url_count < 3:
        score -= 2
        risks.append(f"⚠ URL {url_count}개 < 3 (출처 부족)")
    else:
        notes.append(f"✓ URL {url_count}개")

    # 3. AI disclosure footer 존재
    if "AI" not in html or ("작성" not in html and "보조" not in html):
        score -= 1
        risks.append("⚠ AI disclosure footer 미발견")
    else:
        notes.append("✓ AI disclosure")

    # 4. 제목 길이 (15~60자)
    if not (10 <= len(post.title) <= 60):
        score -= 1
        risks.append(f"⚠ 제목 {len(post.title)}자 (10~60 범위 밖)")
    else:
        notes.append(f"✓ 제목 {len(post.title)}자")

    # 5. 라벨 존재
    if not post.labels:
        score -= 1
        risks.append("⚠ labels 0개")

    return max(0, score), notes, risks


def _llm_self_evaluate(post: BlogPost) -> Optional[int]:
    """LLM 자가 평가 (선택, 빠른 모델 사용)."""
    # 글 미리보기 (HTML 태그 제거)
    text = re.sub(r"<[^>]+>", " ", post.content_html)
    text = re.sub(r"\s+", " ", text).strip()[:2000]

    prompt = HELPFUL_CONTENT_QUESTIONS + f"\n\n---\n평가할 글:\n제목: {post.title}\n\n{text}"

    result = chat(
        "당신은 Google Helpful Content 정책 평가관입니다. 객관적·엄격하게 평가합니다.",
        prompt,
    )
    if not result:
        return None

    # "점수: N/9" 패턴 추출
    m = re.search(r"점수[:\s]+(\d+)/9", result.text)
    if not m:
        m = re.search(r"(\d+)/9", result.text)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def evaluate(post: BlogPost, use_llm: bool = False) -> GateResult:
    """품질 게이트 평가.

    use_llm=False (기본): 휴리스틱만 (빠름, ~1초)
    use_llm=True: LLM 자가 평가 추가 (느림, +30~60초)
    """
    min_score = int(os.environ.get("QUALITY_GATE_MIN_SCORE", "6"))

    score, notes, risks = _heuristic_check(post)
    log.info(f"휴리스틱 점수: {score}/9")
    for n in notes: log.info(f"  {n}")
    for r in risks: log.warning(f"  {r}")

    if use_llm and score >= min_score:
        llm_score = _llm_self_evaluate(post)
        if llm_score is not None:
            log.info(f"LLM 자가 평가: {llm_score}/9")
            score = min(score, llm_score)  # 둘 중 낮은 점수

    passed = score >= min_score
    return GateResult(passed=passed, score=score, notes=notes, risk_flags=risks)


if __name__ == "__main__":
    from common import load_env, setup_logging
    from collectors.rss import collect_all, pick_top
    from generators.article import generate_post

    load_env()
    log = setup_logging("quality_test")

    articles = collect_all(per_source=5)
    top = pick_top(articles, n=5)
    post = generate_post(top)
    if not post:
        log.error("post 생성 실패"); raise SystemExit(1)

    log.info(f"제목: {post.title}, 길이 {post.word_count}자")
    result = evaluate(post, use_llm=False)
    log.info(f"\n결과: {'✅ 통과' if result.passed else '❌ 보류'} (점수 {result.score}/9)")
