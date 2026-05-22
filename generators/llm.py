"""로컬 ollama 우선 + 외부 무료 LLM 5단 폴백.

사용자 명시 "어지간하면 로컬로 써" — ollama 가 살아있으면 100% 로컬.
외부 호출은 ollama 다운 / 모델 응답 빈 경우만.
사용자 자료 0건 전송 (외부 RSS 자료만).
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

import requests

log = logging.getLogger("llm")


@dataclass
class LLMResult:
    text: str
    backend: str
    model: str
    latency_sec: float
    tokens_estimated: int


def _ollama_chat(model: str, system: str, user: str, timeout: int = 600,
                 num_ctx: int = 16384) -> Optional[LLMResult]:
    """로컬 ollama. 응답 빈 경우 None."""
    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    # 시스템 OLLAMA_HOST 가 LAN 바인딩용 0.0.0.0 인 경우 클라이언트 호출은 127.0.0.1
    if "0.0.0.0" in host:
        host = host.replace("0.0.0.0", "127.0.0.1")
    if "://" not in host:
        host = "http://" + host
    t0 = time.time()
    try:
        r = requests.post(
            f"{host}/api/chat",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
                "think": False,  # 사용자 메모리 feedback-local-models-thinking-off 준수
                "options": {
                    "num_ctx": num_ctx,
                    "temperature": 0.7,
                },
            },
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        text = (data.get("message") or {}).get("content", "").strip()
        if not text:
            log.warning(f"ollama {model} 빈 응답")
            return None
        return LLMResult(
            text=text,
            backend="ollama",
            model=model,
            latency_sec=time.time() - t0,
            tokens_estimated=data.get("eval_count", 0),
        )
    except Exception as e:  # noqa: BLE001
        log.warning(f"ollama {model} fail: {e}")
        return None


def _openai_compat_chat(api_base: str, api_key: str, model: str, system: str,
                       user: str, timeout: int = 90) -> Optional[LLMResult]:
    """Cerebras/Groq/SambaNova 공통 OpenAI 호환 endpoint."""
    if not api_key:
        return None
    t0 = time.time()
    try:
        r = requests.post(
            f"{api_base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.7,
                "max_tokens": 4000,
            },
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        text = data["choices"][0]["message"]["content"].strip()
        if not text:
            return None
        return LLMResult(
            text=text,
            backend=api_base.split("://")[-1].split("/")[0],
            model=model,
            latency_sec=time.time() - t0,
            tokens_estimated=data.get("usage", {}).get("completion_tokens", 0),
        )
    except Exception as e:  # noqa: BLE001
        log.warning(f"{api_base} fail: {e}")
        return None


def _cerebras(system: str, user: str) -> Optional[LLMResult]:
    # Cerebras 의 활성 모델 (2026-05 기준). qwen-3-235b-a22b deprecated.
    return _openai_compat_chat(
        "https://api.cerebras.ai/v1",
        os.environ.get("CEREBRAS_KEY", ""),
        "llama-3.3-70b",
        system, user,
    )


def _groq(system: str, user: str) -> Optional[LLMResult]:
    return _openai_compat_chat(
        "https://api.groq.com/openai/v1",
        os.environ.get("GROQ_KEY", ""),
        "llama-3.3-70b-versatile",
        system, user,
    )


def _sambanova(system: str, user: str) -> Optional[LLMResult]:
    return _openai_compat_chat(
        "https://api.sambanova.ai/v1",
        os.environ.get("SAMBANOVA_API_KEY", ""),
        "Meta-Llama-3.3-70B-Instruct",
        system, user,
    )


def _cf_workers(system: str, user: str) -> Optional[LLMResult]:
    """Cloudflare Workers AI."""
    account = os.environ.get("CF_ACCOUNT_ID", "")
    token = os.environ.get("CF_API_TOKEN", "")
    if not account or not token:
        return None
    t0 = time.time()
    try:
        r = requests.post(
            f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/@cf/meta/llama-3.3-70b-instruct-fp8-fast",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": 4000,
            },
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        text = (data.get("result") or {}).get("response", "").strip()
        if not text:
            return None
        return LLMResult(text=text, backend="cf_workers",
                         model="llama-3.3-70b", latency_sec=time.time() - t0,
                         tokens_estimated=0)
    except Exception as e:  # noqa: BLE001
        log.warning(f"cf_workers fail: {e}")
        return None


def _gemini(system: str, user: str) -> Optional[LLMResult]:
    key = os.environ.get("GOOGLE_API_KEY", "")
    if not key:
        return None
    t0 = time.time()
    try:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={key}",
            json={
                "contents": [{"role": "user", "parts": [{"text": f"{system}\n\n{user}"}]}],
                "generationConfig": {"temperature": 0.7, "maxOutputTokens": 4000},
            },
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        if not text:
            return None
        return LLMResult(text=text, backend="gemini",
                         model="gemini-2.5-flash-lite", latency_sec=time.time() - t0,
                         tokens_estimated=0)
    except Exception as e:  # noqa: BLE001
        log.warning(f"gemini fail: {e}")
        return None


# 폴백 체인: 로컬 우선 → 외부 5종
FALLBACK_CHAIN = [
    ("ollama_kr_big", lambda s, u: _ollama_chat(
        os.environ.get("OLLAMA_MODEL_KR_BIG", "qwen36-file:latest"), s, u,
        timeout=900, num_ctx=32768)),
    ("ollama_kr", lambda s, u: _ollama_chat(
        os.environ.get("OLLAMA_MODEL_KR", "sg4nt:latest"), s, u,
        timeout=600, num_ctx=16384)),
    ("cerebras", _cerebras),
    ("groq", _groq),
    ("sambanova", _sambanova),
    ("cf_workers", _cf_workers),
    ("gemini", _gemini),
]


def chat(system: str, user: str) -> Optional[LLMResult]:
    """폴백 체인 순차 호출. 첫 성공 결과 반환."""
    for name, fn in FALLBACK_CHAIN:
        log.info(f"  LLM 시도: {name}")
        result = fn(system, user)
        if result:
            log.info(f"  ✓ {name}: {result.latency_sec:.1f}s, ~{result.tokens_estimated} tok")
            return result
    log.error("모든 LLM 폴백 실패")
    return None


if __name__ == "__main__":
    from common import load_env, setup_logging
    load_env()
    log = setup_logging("llm_test")
    result = chat(
        "당신은 한국어 기술 블로거입니다.",
        "Cerebras 의 Qwen3-235B 모델이 분당 2000 토큰 처리한다는 사실을 한 단락으로 한국어 요약 (3문장)."
    )
    if result:
        print(f"\n=== {result.backend}/{result.model} ===")
        print(result.text)
    else:
        print("FAIL")
