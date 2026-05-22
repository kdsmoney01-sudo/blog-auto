"""메인 파이프라인 — 매일 03:00 KST cron 으로 호출.

흐름:
  RSS 수집 → 상위 5개 픽 → 로컬 LLM 글 생성 → 품질 게이트 →
  vault 저장 → (통과 시) Blogger 자동 게시 → 텔레그램 알림

사용 예:
  python pipeline.py            # 일반 실행 (.env DRY_RUN 따름)
  python pipeline.py --dry-run  # 강제 더미 (게시 X, vault drafts 만)
  python pipeline.py --publish  # 강제 발행 (.env DRY_RUN 무시)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from common import load_env, setup_logging, telegram_send  # noqa: E402
from collectors.rss import collect_all, pick_top  # noqa: E402
from generators.article import generate_post  # noqa: E402
from generators.quality_gate import evaluate  # noqa: E402
from storage import vault  # noqa: E402


def run(dry_run: bool, publish_force: bool, use_llm_gate: bool = False) -> dict:
    log = logging.getLogger("pipeline")
    started = datetime.now()
    result = {
        "started_at": started.isoformat(),
        "dry_run": dry_run,
        "ok": False,
        "stage": "init",
        "post_title": None,
        "blogger_post_id": None,
        "blogger_url": None,
        "quality_score": None,
        "llm_backend": None,
        "error": None,
    }

    try:
        # 1. RSS 수집
        result["stage"] = "collect"
        log.info("=" * 60)
        log.info(f"파이프라인 시작 (dry_run={dry_run}, publish_force={publish_force})")
        log.info("=" * 60)
        log.info("1. RSS 수집")
        articles = collect_all(per_source=5)
        log.info(f"   수집 {len(articles)}개")
        vault.save_raw_rss(articles)

        # 2. 상위 픽
        result["stage"] = "pick"
        top = pick_top(articles, n=5, hours_window=48)
        log.info(f"2. 상위 픽 {len(top)}개")
        if len(top) < 3:
            raise RuntimeError(f"상위 픽 {len(top)} < 3 (RSS 자료 부족)")

        # 3. LLM 글 생성
        result["stage"] = "generate"
        log.info("3. LLM 글 생성 (로컬 ollama 우선)")
        post = generate_post(top)
        if not post:
            raise RuntimeError("LLM 글 생성 실패 (모든 폴백 실패)")
        result["post_title"] = post.title
        result["llm_backend"] = f"{post.llm_backend}/{post.llm_model}"
        log.info(f"   제목: {post.title}")
        log.info(f"   길이: {post.word_count}자")
        log.info(f"   백엔드: {result['llm_backend']}")

        # 4. 품질 게이트
        result["stage"] = "quality_gate"
        log.info("4. 품질 게이트")
        gate = evaluate(post, use_llm=use_llm_gate)
        result["quality_score"] = gate.score
        log.info(f"   점수 {gate.score}/9 ({'통과' if gate.passed else '보류'})")

        # 5. vault 저장 (통과/보류 모두)
        result["stage"] = "vault_save"
        status_for_vault = "draft" if (dry_run or not gate.passed) else "published"
        vault_path = vault.save_post(post, status=status_for_vault)
        log.info(f"5. vault 저장: {vault_path}")

        # 6. 발행 (gate 통과 + (publish_force 또는 not dry_run))
        if not gate.passed:
            log.warning("품질 게이트 보류 → 발행 SKIP (drafts 만 저장)")
            telegram_send(
                f"📝 블로그 글 생성 (품질 보류)\n"
                f"제목: {post.title}\n"
                f"점수: {gate.score}/9\n"
                f"리스크: {', '.join(gate.risk_flags) if gate.risk_flags else '없음'}\n"
                f"vault: {vault_path.name}",
                channel="news",
            )
        elif dry_run and not publish_force:
            log.info("DRY_RUN → 발행 SKIP")
            telegram_send(
                f"🧪 블로그 글 DRY_RUN 생성\n"
                f"제목: {post.title}\n"
                f"백엔드: {result['llm_backend']}\n"
                f"점수: {gate.score}/9\n"
                f"vault: {vault_path.name}",
                channel="news",
            )
        else:
            result["stage"] = "publish"
            log.info("6. Blogger 자동 발행")
            from publishers.blogger import publish_post
            published = publish_post(
                title=post.title,
                content_html=post.content_html,
                labels=post.labels,
                is_draft=False,
            )
            result["blogger_post_id"] = published.get("id")
            result["blogger_url"] = published.get("url")
            log.info(f"   ✓ 발행: {published.get('url')}")

            # vault 재저장 (published 폴더로 + Blogger ID 박음)
            vault.save_post(post, blogger_post_id=published.get("id"),
                            blogger_url=published.get("url"), status="published")

            telegram_send(
                f"✅ 블로그 새 글 발행\n"
                f"제목: {post.title}\n"
                f"URL: {published.get('url')}\n"
                f"백엔드: {result['llm_backend']}\n"
                f"점수: {gate.score}/9",
                channel="news",
            )

        # 7. analytics 기록
        vault.save_analytics_entry({
            "type": "publish_attempt",
            "title": post.title,
            "score": gate.score,
            "passed": gate.passed,
            "published": result["blogger_post_id"] is not None,
            "blogger_url": result["blogger_url"],
            "llm_backend": result["llm_backend"],
            "word_count": post.word_count,
        })

        result["ok"] = True
        result["stage"] = "done"

    except Exception as e:  # noqa: BLE001
        result["error"] = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        log.error(f"파이프라인 실패 at stage={result['stage']}: {e}")
        log.error(traceback.format_exc())
        telegram_send(
            f"🚨 블로그 파이프라인 실패\n"
            f"단계: {result['stage']}\n"
            f"에러: {type(e).__name__}: {str(e)[:200]}",
            channel="news",
        )

    elapsed = (datetime.now() - started).total_seconds()
    result["elapsed_sec"] = elapsed
    log.info("=" * 60)
    log.info(f"파이프라인 종료 (ok={result['ok']}, {elapsed:.0f}초)")
    log.info("=" * 60)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="강제 더미 (게시 X)")
    parser.add_argument("--publish", action="store_true", help="강제 발행 (.env DRY_RUN 무시)")
    parser.add_argument("--llm-gate", action="store_true", help="LLM 자가 평가 추가 (느림)")
    args = parser.parse_args()

    load_env()
    setup_logging("pipeline")

    env_dry = os.environ.get("DRY_RUN", "false").lower() == "true"
    dry_run = args.dry_run or (env_dry and not args.publish)
    publish_force = args.publish

    result = run(dry_run=dry_run, publish_force=publish_force, use_llm_gate=args.llm_gate)

    # 결과 JSON 마지막 줄 출력 (cron 로그 파싱용)
    print("\n=== RESULT ===")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
