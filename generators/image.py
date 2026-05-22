"""ComfyUI SDXL 이미지 자동 생성 (로컬, 사용자 "어지간하면 로컬" 의도).

흐름:
  1. 글 주제·키워드 → 영어 prompt
  2. ComfyUI API workflow JSON 호출
  3. 생성된 이미지 받아오기
  4. base64 인코딩 → Blogger 본문에 인라인 <img>
"""
from __future__ import annotations

import base64
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger("image")


# SDXL Base 1.0 워크플로우 (API JSON 형식)
def _workflow(prompt: str, negative: str = "low quality, blurry, watermark",
              width: int = 1024, height: int = 1024, steps: int = 20,
              cfg: float = 7.0, model: str = "sd_xl_base_1.0.safetensors") -> dict:
    seed = int(time.time() * 1000) % (2**31)
    return {
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": "euler",
                "scheduler": "normal",
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


def generate(prompt: str, timeout: int = 600) -> Optional[bytes]:
    """ComfyUI 큐 호출 + 이미지 PNG bytes 반환. 실패 시 None."""
    if not is_alive():
        log.warning("ComfyUI 미가동 (COMFYUI_HOST=%s)", _host())
        return None

    host = _host()
    client_id = str(uuid.uuid4())
    wf = _workflow(prompt)

    try:
        # 큐 등록
        r = requests.post(f"{host}/prompt", json={"prompt": wf, "client_id": client_id},
                          timeout=15)
        r.raise_for_status()
        prompt_id = r.json()["prompt_id"]
        log.info(f"ComfyUI prompt_id={prompt_id}, prompt='{prompt[:60]}...'")

        # 폴링 (history)
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


def upload_to_blogger(image_bytes: bytes, alt: str = "cover") -> str:
    """간단 — base64 인라인 데이터 URI 반환. 작은 이미지에만."""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return f"data:image/png;base64,{b64}"


def upload_to_github_raw(image_bytes: bytes, filename: str) -> Optional[str]:
    """GitHub repo 에 이미지 push 후 raw.githubusercontent.com URL 반환.

    사용자 자료 0건 (외부 RSS 기반 가공 이미지만). GitHub 무료 + 영구.
    """
    user = os.environ.get("GITHUB_USERNAME")
    token = os.environ.get("GITHUB_TOKEN")
    repo_url = os.environ.get("GITHUB_REPO_URL", "")
    if not user or not token:
        log.warning("GitHub 토큰 없음")
        return None
    # repo_url 에서 repo 이름 추출
    repo_name = repo_url.rstrip("/").split("/")[-1] or "blog-auto"

    path = f"images/{filename}"
    api = f"https://api.github.com/repos/{user}/{repo_name}/contents/{path}"
    content_b64 = base64.b64encode(image_bytes).decode("ascii")

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

    # 기존 파일 SHA (덮어쓰기 시 필요)
    sha = None
    try:
        r = requests.get(api, headers=headers, timeout=15)
        if r.status_code == 200:
            sha = r.json().get("sha")
    except Exception:
        pass

    body = {
        "message": f"image: {filename}",
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


def topic_to_prompt(title: str, summary: str = "") -> str:
    """글 제목·요약 → SDXL 영문 프롬프트.

    저작권 안전: 사람·실재 인물·로고 X. 추상 시각 메타포.
    """
    # 한국어 제목에서 핵심 키워드 추출은 LLM 호출이 본격적이지만,
    # 간단히 영문 키워드 베이스 + 추상 스타일 강제
    base = "futuristic minimal abstract illustration, AI technology, neural network nodes, "
    base += "blue gradient background, glowing data streams, professional editorial style, "
    base += "no text, no faces, no logos, clean vector style, high quality"
    # 한국 IT/AI 트렌드 글이라 추상 + 깔끔
    return base


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
