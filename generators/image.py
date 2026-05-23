"""ComfyUI 이미지 자동 생성 (로컬, 사용자 "어지간하면 로컬" 의도).

흐름:
  1. 글 주제·키워드 -> 영어 prompt (동적, 키워드 매핑 + 스타일 다양화)
  2. ComfyUI API workflow JSON 호출 (Juggernaut XL v9, 16:9, dpmpp_2m_sde + karras, steps 30)
  3. 생성된 이미지 받아오기
  4. GitHub REST API push -> raw URL
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import random
import time
import uuid
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger("image")


DEFAULT_NEGATIVE = (
    "low quality, blurry, pixelated, jpeg artifacts, compression artifacts, "
    "watermark, signature, text, letters, logos, "
    "deformed, ugly, bad anatomy, extra limbs, mutated hands, distorted face, "
    "cluttered, busy background, oversaturated, harsh shadows, "
    "amateur, drawing, sketch, cartoon, anime, illustration noise, "
    "low resolution, rendering glitches, jpeg compression"
)


def _host() -> str:
    h = os.environ.get("COMFYUI_HOST", "http://127.0.0.1:8188")
    if "0.0.0.0" in h:
        h = h.replace("0.0.0.0", "127.0.0.1")
    if "://" not in h:
        h = "http://" + h
    return h


def is_alive(timeout: int = 3) -> bool:
    try:
        r = requests.get(f"{_host()}/system_stats", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def _resolve_model(preferred: str) -> str:
    """Return preferred checkpoint if ComfyUI actually recognizes it.

    ComfyUI scans checkpoints folder ONCE at startup. New files added later are
    invisible until process restart. So disk file presence is not enough; we
    must ask ComfyUI itself.
    """
    try:
        r = requests.get(f"{_host()}/object_info/CheckpointLoaderSimple", timeout=4)
        if r.ok:
            data = r.json()
            avail = (
                data.get("CheckpointLoaderSimple", {})
                .get("input", {})
                .get("required", {})
                .get("ckpt_name", [[], {}])[0]
            )
            if preferred in avail:
                return preferred
            log.warning(
                f"checkpoint {preferred} not in ComfyUI list "
                f"({len(avail)} known: {avail[:3]}) -> fallback sd_xl_base_1.0. "
                f"Restart ComfyUI to pick up newly downloaded models."
            )
    except Exception as e:  # noqa: BLE001
        log.warning(f"ComfyUI checkpoint list check failed: {e}")
    return "sd_xl_base_1.0.safetensors"


def _workflow(prompt: str, negative: str = DEFAULT_NEGATIVE,
              width: int = 1344, height: int = 768, steps: int = 30,
              cfg: float = 5.5, model: Optional[str] = None) -> dict:
    if model is None:
        preferred = os.environ.get("IMAGE_MODEL", "juggernautXL_v9.safetensors")
        model = _resolve_model(preferred)
    seed = int(time.time() * 1000) % (2 ** 31)
    return {
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": "dpmpp_2m_sde",
                "scheduler": "karras",
                "denoise": 1.0,
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
            },
        },
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": model}},
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["4", 1]},
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative, "clip": ["4", 1]},
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "blog_auto", "images": ["8", 0]},
        },
    }


def generate(prompt: str, timeout: int = 600) -> Optional[bytes]:
    """ComfyUI 큐 호출 + 이미지 PNG bytes 반환. 실패 시 None."""
    if not is_alive():
        log.warning("ComfyUI 미가동 (COMFYUI_HOST=%s)", _host())
        return None

    host = _host()
    client_id = str(uuid.uuid4())
    wf = _workflow(prompt)

    try:
        r = requests.post(f"{host}/prompt", json={"prompt": wf, "client_id": client_id},
                          timeout=15)
        r.raise_for_status()
        prompt_id = r.json()["prompt_id"]
        log.info(f"ComfyUI prompt_id={prompt_id}, prompt='{prompt[:60]}...'")

        t0 = time.time()
        while time.time() - t0 < timeout:
            time.sleep(2)
            h = requests.get(f"{host}/history/{prompt_id}", timeout=10).json()
            if prompt_id in h:
                outputs = h[prompt_id].get("outputs", {})
                for node_id, node_out in outputs.items():
                    if "images" not in node_out:
                        continue
                    img_info = node_out["images"][0]
                    img_url = (
                        f"{host}/view?filename={img_info['filename']}"
                        f"&subfolder={img_info.get('subfolder', '')}"
                        f"&type={img_info.get('type', 'output')}"
                    )
                    img_resp = requests.get(img_url, timeout=30)
                    img_resp.raise_for_status()
                    log.info(f"ComfyUI 이미지 생성 완료 ({time.time() - t0:.0f}초)")
                    return img_resp.content
        log.warning(f"ComfyUI timeout ({timeout}초)")
        return None
    except Exception as e:  # noqa: BLE001
        log.warning(f"ComfyUI fail: {e}")
        return None


def upload_to_blogger(image_bytes: bytes) -> str:
    """base64 인라인 데이터 URI. 작은 이미지 전용."""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return f"data:image/png;base64,{b64}"


# commit message 위장 (사람 git 패턴, 자동화 명백한 "image: <filename>" 회피)
_COMMIT_MESSAGES = [
    "Add asset", "Update media", "New resource", "Asset update",
    "Update", "Add cover", "New image", "Update post asset",
    "Refresh media", "Add", "Resource", "Update assets",
]


def upload_to_github_raw(image_bytes: bytes, filename: str) -> Optional[str]:
    user = os.environ.get("GITHUB_USERNAME")
    token = os.environ.get("GITHUB_TOKEN")
    repo_url = os.environ.get("GITHUB_REPO_URL", "")
    if not user or not token:
        log.warning("GitHub 토큰 없음")
        return None
    repo_name = repo_url.rstrip("/").split("/")[-1] or "blog-auto"

    path = f"images/{filename}"
    api = f"https://api.github.com/repos/{user}/{repo_name}/contents/{path}"
    content_b64 = base64.b64encode(image_bytes).decode("ascii")

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

    sha = None
    try:
        r = requests.get(api, headers=headers, timeout=15)
        if r.status_code == 200:
            sha = r.json().get("sha")
    except Exception:
        pass

    body = {
        "message": random.choice(_COMMIT_MESSAGES),
        "content": content_b64,
        "branch": "main",
    }
    if sha:
        body["sha"] = sha

    try:
        r = requests.put(api, headers=headers, json=body, timeout=30)
        if r.status_code in (200, 201):
            raw_url = f"https://raw.githubusercontent.com/{user}/{repo_name}/main/{path}"
            log.info(f"GitHub 이미지 push OK: {raw_url}")
            return raw_url
        else:
            log.warning(f"GitHub push fail {r.status_code}: {r.text[:200]}")
            return None
    except Exception as e:  # noqa: BLE001
        log.warning(f"GitHub push exception: {e}")
        return None


# 한국어 IT/AI 글 제목 키워드 -> SDXL 영문 비주얼 메타포
# 순서 중요 — 구체적인 키 먼저, "AI/인공지능" 같은 광범 키는 마지막 fallback
_KEYWORD_MAP: list[tuple[tuple[str, ...], str]] = [
    (("DeepSeek", "GPT", "Claude", "Gemini", "Llama"), "abstract neural network constellation, glowing nodes, premium tech editorial"),
    (("엔비디아", "TSMC", "삼성전자", "SK하이닉스"), "advanced semiconductor chip macro, circuit board detail, blue glow, studio light"),
    (("자율주행", "전기차", "EV", "테슬라"), "futuristic electric vehicle silhouette, urban environment, soft headlight glow"),
    (("암호화폐", "코인", "비트코인", "블록체인", "NFT"), "abstract blockchain network, golden geometric nodes, dark backdrop"),
    (("메타버스", "VR", "AR", "혼합현실"), "immersive virtual reality concept, holographic light streams, deep blue"),
    (("로봇", "robot", "RPA", "휴머노이드"), "sleek industrial robotics arm, soft studio light, premium product photography"),
    (("오픈소스", "open source", "GitHub"), "open source code ecosystem, flowing text constellation, terminal aesthetic"),
    (("GPU", "반도체", "칩", "프로세서"), "semiconductor wafer macro, intricate circuit pattern, blue lighting"),
    (("보안", "해킹", "사이버", "cyber", "랜섬웨어"), "cybersecurity shield concept, geometric protective dome, dark blue lines"),
    (("바이오", "신약", "헬스케어", "의료"), "abstract DNA helix, medical lab aesthetics, soft blue cyan glow"),
    (("자동화", "automation", "워크플로우"), "modern automated factory, mechanical precision, soft industrial light"),
    (("클라우드", "cloud", "데이터센터", "서버"), "cloud computing infrastructure, isometric server racks, ambient blue"),
    (("스타트업", "투자", "벤처", "유니콘", "IPO"), "modern business cityscape at dawn, glass skyscraper reflections"),
    (("빅데이터", "데이터 분석", "분석"), "abstract big data visualization, flowing particle streams, gradient mesh"),
    (("교육", "학습", "강의", "AI 교육"), "open book with floating light particles, modern learning concept"),
    (("게임", "e스포츠", "esports"), "immersive game studio, ambient RGB lighting, modern aesthetics"),
    (("도구화", "전환", "재정의", "혁신", "변화", "패러다임"), "abstract transformation, geometric morphing shapes, light to dark gradient"),
    (("인재", "직업", "일자리", "고용", "재택"), "modern professional workspace, minimal desk, soft natural window light"),
    (("정책", "규제", "법", "규제완화"), "modern government building, clean architectural lines, soft daylight"),
    (("가격", "비용", "수익", "투자"), "abstract financial visualization, ascending light curves, dark editorial"),
    (("머신러닝", "딥러닝", "ML", "신경망"), "abstract neural network mesh, soft glowing nodes, professional tech editorial"),
    (("LLM", "추론", "모델", "inference"), "abstract language model concept, flowing text particles, deep blue"),
    (("인공지능", "AI"), "abstract intelligence visualization, soft glow particles, premium editorial"),
]


# 글마다 다른 비주얼 톤
_STYLE_VARIANTS = [
    "editorial photography, cinematic lighting, shallow depth of field, premium magazine cover",
    "modern flat design illustration, soft gradient, isometric composition, clean minimal",
    "abstract conceptual photography, dreamy bokeh, soft pastel palette, editorial aesthetic",
    "futuristic 3d render, octane render, premium product photography, soft studio light",
    "minimalist tech editorial, negative space, monochromatic blue palette, refined composition",
    "premium tech magazine cover, dramatic side lighting, high end commercial photography",
]


def topic_to_prompt(title: str, summary: str = "") -> str:
    """글 제목·요약 -> SDXL 영문 prompt (동적, 키워드 매핑 + 스타일 다양화).

    Determinism: title hash 기준으로 같은 글 = 같은 스타일 (재현성).
    """
    text = (title + " " + summary).strip()

    visual = "abstract technology concept, soft glow, premium editorial"
    for keywords, metaphor in _KEYWORD_MAP:
        if any(k.lower() in text.lower() for k in keywords):
            visual = metaphor
            break

    if text:
        h = int(hashlib.md5(text.encode("utf-8", errors="ignore")).hexdigest(), 16)
        style = _STYLE_VARIANTS[h % len(_STYLE_VARIANTS)]
    else:
        style = _STYLE_VARIANTS[0]

    prompt = (
        f"{visual}, {style}, "
        f"8k uhd, sharp focus, professional, refined, sophisticated, "
        f"no text, no logos, no faces, no humans"
    )
    return prompt


if __name__ == "__main__":
    from common import load_env, setup_logging
    load_env()
    log = setup_logging("image_test")
    log.info(f"ComfyUI alive: {is_alive()}")
    if is_alive():
        img = generate(topic_to_prompt("AI 트렌드 테스트"))
        if img:
            out = Path(__file__).parent.parent / "_workspace" / "test_image.png"
            out.parent.mkdir(exist_ok=True)
            out.write_bytes(img)
            log.info(f"테스트 이미지 저장: {out} ({len(img)/1024:.0f} KB)")
        else:
            log.error("이미지 생성 실패")
