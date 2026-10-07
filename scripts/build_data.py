#!/usr/bin/env python3
"""Fetch top recent X posts and classify each post with TypeSafe Jev."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from authlib.integrations.httpx_client import OAuth1Client


X_SEARCH_URL = "https://api.x.com/2/tweets/search/recent"
TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
QUERY = '(Bybit OR "Ben Zhou" OR @benbybit) -is:retweet'
MAX_POSTS = 100
CHUNK_SIZE = 20


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def fetch_posts() -> tuple[list[dict], datetime, datetime]:
    end = (datetime.now(timezone.utc) - timedelta(seconds=15)).replace(microsecond=0)
    start = end - timedelta(hours=24)
    client = OAuth1Client(
        client_id=required("X_API_KEY"),
        client_secret=required("X_API_SECRET"),
        token=required("X_ACCESS_TOKEN"),
        token_secret=required("X_ACCESS_TOKEN_SECRET"),
        timeout=45,
    )
    try:
        response = client.get(
            X_SEARCH_URL,
            params={
                "query": QUERY,
                "start_time": start.isoformat().replace("+00:00", "Z"),
                "end_time": end.isoformat().replace("+00:00", "Z"),
                "max_results": MAX_POSTS,
                "sort_order": "relevancy",
                "tweet.fields": "created_at,author_id,public_metrics,lang,conversation_id",
                "expansions": "author_id",
                "user.fields": "name,username,public_metrics,verified",
            },
        )
    finally:
        client.close()
    if response.status_code >= 400:
        raise RuntimeError(f"X API returned HTTP {response.status_code}: {response.text[:500]}")

    payload = response.json()
    users = {
        str(user["id"]): user
        for user in payload.get("includes", {}).get("users", [])
        if user.get("id")
    }
    posts = []
    for item in payload.get("data", []):
        metrics = item.get("public_metrics", {})
        author = users.get(str(item.get("author_id", "")), {})
        username = author.get("username", "")
        posts.append(
            {
                "id": str(item["id"]),
                "text": item.get("text", "").strip(),
                "created_at": item.get("created_at", ""),
                "lang": item.get("lang", ""),
                "author_name": author.get("name", username or "Unknown"),
                "username": username,
                "followers": author.get("public_metrics", {}).get("followers_count"),
                "views": metrics.get("impression_count"),
                "likes": metrics.get("like_count"),
                "retweets": metrics.get("retweet_count"),
                "replies": metrics.get("reply_count"),
                "quotes": metrics.get("quote_count"),
                "url": (
                    f"https://x.com/{username}/status/{item['id']}"
                    if username
                    else f"https://x.com/i/web/status/{item['id']}"
                ),
            }
        )

    posts.sort(
        key=lambda post: (
            post.get("views") or 0,
            (post.get("likes") or 0)
            + (post.get("retweets") or 0)
            + (post.get("replies") or 0)
            + (post.get("quotes") or 0),
        ),
        reverse=True,
    )
    return posts, start, end


def typesafe_request(state: list[dict], questions: dict) -> dict:
    body = json.dumps(
        {"state": {"posts": state}, "model": "jev-latest", "questions": questions}
    ).encode("utf-8")
    request = Request(
        TYPESAFE_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {required('TYPESAFE_API_KEY')}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=90) as response:
            return json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"TypeSafe API returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"TypeSafe API request failed: {exc.reason}") from exc


def classify_posts(posts: list[dict]) -> tuple[str, dict]:
    model = ""
    usage = {"input_tokens": 0, "output_tokens": 0}
    for offset in range(0, len(posts), CHUNK_SIZE):
        chunk = posts[offset : offset + CHUNK_SIZE]
        state = [
            {
                "post_id": post["id"],
                "text": post["text"],
                "author": f"@{post['username']}" if post["username"] else post["author_name"],
            }
            for post in chunk
        ]
        questions = {}
        for index, post in enumerate(chunk):
            questions[f"post_{index}"] = {
                "type": "choice",
                "instructions": {
                    "decision": "Classify the post's sentiment toward Bybit or Ben Zhou.",
                    "subject": "Bybit and its co-founder and CEO Ben Zhou (@benbybit)",
                    "post": f"Read only `posts[{index}].text` for this decision.",
                },
                "criteria": {
                    "Positive": (
                        "Favorable, supportive, celebratory, promotional, or neutral-to-favorable "
                        "coverage that does not criticize or harm the subject."
                    ),
                    "Negative": (
                        "Criticism, complaint, allegation, warning, reported harm or failure, "
                        "mockery, distrust, or an unfavorable comparison involving the subject."
                    ),
                },
            }

        result = typesafe_request(state, questions)
        model = result.get("model", model)
        chunk_usage = result.get("usage", {})
        usage["input_tokens"] += int(chunk_usage.get("input_tokens", 0))
        usage["output_tokens"] += int(chunk_usage.get("output_tokens", 0))
        answers = result.get("answers", {})
        for index, post in enumerate(chunk):
            answer = answers.get(f"post_{index}", {})
            choice = answer.get("choice")
            if choice not in {"Positive", "Negative"}:
                raise RuntimeError(f"Invalid TypeSafe answer for post {post['id']}: {answer}")
            post["sentiment"] = choice
            post["sentiment_confidence"] = answer.get("confidence")
            post["sentiment_probabilities"] = answer.get("probabilities", {})
    return model, usage


def main() -> int:
    posts, start, end = fetch_posts()
    if not posts:
        raise RuntimeError("X search returned no posts")
    model, usage = classify_posts(posts)
    output = {
        "generated_at": end.isoformat().replace("+00:00", "Z"),
        "window_start": start.isoformat().replace("+00:00", "Z"),
        "window_end": end.isoformat().replace("+00:00", "Z"),
        "query": QUERY,
        "search_order": "X API relevancy, then page default by views",
        "typesafe_model": model,
        "typesafe_usage": usage,
        "count": len(posts),
        "posts": posts,
    }
    output_path = Path(__file__).resolve().parents[1] / "data" / "posts.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "posts": len(posts),
                "positive": sum(post["sentiment"] == "Positive" for post in posts),
                "negative": sum(post["sentiment"] == "Negative" for post in posts),
                "typesafe_model": model,
                "output": str(output_path),
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
