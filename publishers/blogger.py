"""Blogger API v3 자동 게시 모듈."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/blogger"]


def _credentials() -> Credentials:
    token_path = Path(os.environ["TOKEN_PATH"])
    client_json_path = Path(os.environ["OAUTH_CLIENT_JSON_PATH"])

    with open(token_path, encoding="utf-8") as f:
        token = json.load(f)
    with open(client_json_path, encoding="utf-8") as f:
        client = json.load(f)["installed"]

    return Credentials(
        token=token.get("token"),
        refresh_token=token["refresh_token"],
        token_uri=client["token_uri"],
        client_id=client["client_id"],
        client_secret=client["client_secret"],
        scopes=token.get("scopes", SCOPES),
    )


def _service():
    return build("blogger", "v3", credentials=_credentials())


def get_blog_info() -> dict:
    blog_id = os.environ["BLOGGER_BLOG_ID"]
    return _service().blogs().get(blogId=blog_id).execute()


def list_posts(limit: int = 10) -> list:
    blog_id = os.environ["BLOGGER_BLOG_ID"]
    result = _service().posts().list(blogId=blog_id, maxResults=limit).execute()
    return result.get("items", [])


def publish_post(title: str, content_html: str, labels: list[str] | None = None,
                 is_draft: bool = False) -> dict:
    """글 1편 발행. is_draft=True 면 비공개 초안만."""
    blog_id = os.environ["BLOGGER_BLOG_ID"]
    body = {
        "kind": "blogger#post",
        "blog": {"id": blog_id},
        "title": title,
        "content": content_html,
    }
    if labels:
        body["labels"] = labels

    service = _service()
    result = service.posts().insert(
        blogId=blog_id,
        body=body,
        isDraft=is_draft,
    ).execute()
    return result


def delete_post(post_id: str) -> None:
    blog_id = os.environ["BLOGGER_BLOG_ID"]
    _service().posts().delete(blogId=blog_id, postId=post_id).execute()


if __name__ == "__main__":
    # 검증
    from common import load_env, setup_logging

    load_env()
    log = setup_logging("blogger_test")
    info = get_blog_info()
    log.info(f"Blog: {info['name']} ({info['url']}) — 글 {info['posts']['totalItems']}편")
    posts = list_posts(5)
    log.info(f"최근 글 {len(posts)}편:")
    for p in posts:
        log.info(f"  - {p['title']} ({p['url']})")
