#!/usr/bin/env python3

import argparse
import json
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/127 Safari/537.36"
BLOCK_MARKERS = (
    "target url returned error 403",
    "you've been blocked by network security",
    "you have been blocked by network security",
    "to continue, log in to your reddit account",
    "use your developer token",
    "access denied",
)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def looks_blocked(text):
    lower = (text or "").lower()
    return any(marker in lower for marker in BLOCK_MARKERS)


def fetch_text(url, timeout=20, headers=None, max_bytes=1_500_000):
    started = time.time()
    req = Request(url, headers={"User-Agent": UA, "Accept": "*/*", **(headers or {})})
    try:
        with urlopen(req, timeout=timeout) as r:
            raw = r.read(max_bytes)
            text = raw.decode("utf-8", "replace")
            return {
                "ok": 200 <= r.status < 300,
                "status": r.status,
                "url": r.geturl(),
                "content_type": r.headers.get("content-type", ""),
                "elapsed_seconds": round(time.time() - started, 3),
                "bytes": len(raw),
                "text": text,
                "error": None,
            }
    except Exception as exc:
        return {
            "ok": False,
            "status": None,
            "url": url,
            "content_type": "",
            "elapsed_seconds": round(time.time() - started, 3),
            "bytes": 0,
            "text": "",
            "error": repr(exc),
        }


def try_json(record):
    if not record.get("text"):
        return None
    try:
        return json.loads(record["text"])
    except Exception:
        return None


def platform_for(url):
    host = (urlparse(url).hostname or "").lower()
    if host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        return "x"
    if host.endswith("reddit.com") or host == "redd.it":
        return "reddit"
    if host.endswith("youtube.com") or host == "youtu.be":
        return "youtube"
    if host.endswith("bilibili.com") or host == "b23.tv":
        return "bilibili"
    return "web"


def x_identity(url):
    m = re.search(r"/(?:i/web/)?([^/]+)/status/(\d+)", urlparse(url).path)
    if not m:
        m = re.search(r"/status/(\d+)", urlparse(url).path)
        return (None, m.group(1)) if m else (None, None)
    return m.group(1), m.group(2)


def normalize_fx(obj):
    if not isinstance(obj, dict):
        return None
    tweet = obj.get("tweet") or obj.get("status")
    if not isinstance(tweet, dict):
        return None
    author = tweet.get("author") or {}
    raw_text = tweet.get("raw_text")
    if isinstance(raw_text, dict):
        raw_text = raw_text.get("text")
    return {
        "id": tweet.get("id"),
        "text": tweet.get("text") or raw_text,
        "url": tweet.get("url"),
        "created_at": tweet.get("created_at"),
        "author": {
            "name": author.get("name"),
            "screen_name": author.get("screen_name") or author.get("username"),
            "followers": author.get("followers"),
        },
        "metrics": {
            "views": tweet.get("views"),
            "likes": tweet.get("likes"),
            "retweets": tweet.get("retweets"),
            "replies": tweet.get("replies"),
            "quotes": tweet.get("quotes"),
            "bookmarks": tweet.get("bookmarks"),
        },
        "media": tweet.get("media"),
        "quote": tweet.get("quote"),
    }


def attempt_meta(source, endpoint, rec, valid=None):
    return {
        "source": source,
        "endpoint": endpoint,
        "ok": rec["ok"],
        "valid_content": valid,
        "status": rec["status"],
        "elapsed_seconds": rec["elapsed_seconds"],
        "bytes": rec["bytes"],
        "error": rec["error"],
    }


def read_x(url, timeout):
    handle, tid = x_identity(url)
    attempts = []
    if tid:
        endpoints = [("fxtwitter", f"https://api.fxtwitter.com/status/{tid}")]
        if handle:
            endpoints.append(("vxtwitter", f"https://api.vxtwitter.com/{handle}/status/{tid}"))
        endpoints.append(("jina", f"https://r.jina.ai/https://x.com/{handle or 'i'}/status/{tid}"))
    else:
        endpoints = [("jina", f"https://r.jina.ai/{url}")]

    for source, endpoint in endpoints:
        rec = fetch_text(endpoint, timeout=timeout)
        obj = try_json(rec)
        normalized = normalize_fx(obj) if source in {"fxtwitter", "vxtwitter"} else None
        valid_jina = rec["ok"] and len(rec["text"].strip()) > 100 and not looks_blocked(rec["text"])
        attempts.append(attempt_meta(source, endpoint, rec, valid_jina if source == "jina" else None))
        if normalized and normalized.get("text"):
            return {
                "platform": "x",
                "ok": True,
                "source": source,
                "requested_url": url,
                "normalized": normalized,
                "attempts": attempts,
            }
        if source == "jina" and valid_jina:
            return {
                "platform": "x",
                "ok": True,
                "source": "jina",
                "requested_url": url,
                "normalized": {"text": rec["text"], "url": url},
                "attempts": attempts,
            }
    return {"platform": "x", "ok": False, "source": None, "requested_url": url, "attempts": attempts}


def reddit_old_url(url):
    p = urlparse(url)
    host = p.hostname or "www.reddit.com"
    if host.endswith("reddit.com"):
        host = "old.reddit.com"
    return urlunparse((p.scheme or "https", host, p.path, "", p.query, ""))


def read_reddit(url, timeout):
    attempts = []
    clean = url.split("?", 1)[0].rstrip("/") + "/"
    old = reddit_old_url(clean)
    candidates = [
        ("reddit_json", clean + ".json"),
        ("reddit_rss", clean + ".rss"),
        ("old_reddit_rss", old + ".rss"),
        ("jina_old_reddit", f"https://r.jina.ai/{old}"),
        ("jina", f"https://r.jina.ai/{url}"),
    ]

    for source, endpoint in candidates:
        rec = fetch_text(endpoint, timeout=timeout, headers={"Accept-Language": "en-US,en;q=0.9"})
        blocked = looks_blocked(rec["text"])
        valid = False
        normalized = None
        if source == "reddit_json":
            obj = try_json(rec)
            valid = obj is not None and not blocked
            normalized = obj if valid else None
        elif source in {"reddit_rss", "old_reddit_rss"}:
            lower = rec["text"].lower()
            valid = rec["ok"] and not blocked and len(rec["text"]) > 200 and ("<rss" in lower or "<feed" in lower)
            normalized = {"text": rec["text"], "url": endpoint, "format": "rss"} if valid else None
        else:
            valid = rec["ok"] and not blocked and len(rec["text"].strip()) > 150
            normalized = {"text": rec["text"], "url": url} if valid else None

        attempts.append(attempt_meta(source, endpoint, rec, valid))
        if valid:
            return {
                "platform": "reddit",
                "ok": True,
                "source": source,
                "requested_url": url,
                "normalized": normalized,
                "attempts": attempts,
            }

    return {"platform": "reddit", "ok": False, "source": None, "requested_url": url, "attempts": attempts}


def read_youtube(url, timeout):
    attempts = []
    started = time.time()
    try:
        proc = subprocess.run(
            ["yt-dlp", "--dump-single-json", "--skip-download", "--no-warnings", url],
            capture_output=True,
            text=True,
            timeout=max(timeout, 30),
        )
        elapsed = round(time.time() - started, 3)
        attempts.append({
            "source": "yt-dlp",
            "ok": proc.returncode == 0,
            "valid_content": proc.returncode == 0 and bool(proc.stdout.strip()),
            "status": proc.returncode,
            "elapsed_seconds": elapsed,
            "bytes": len(proc.stdout.encode()),
            "error": proc.stderr[-2000:] if proc.returncode else None,
        })
        if proc.returncode == 0 and proc.stdout.strip():
            obj = json.loads(proc.stdout)
            keep = {
                "id": obj.get("id"),
                "title": obj.get("title"),
                "description": obj.get("description"),
                "channel": obj.get("channel") or obj.get("uploader"),
                "channel_id": obj.get("channel_id") or obj.get("uploader_id"),
                "duration": obj.get("duration"),
                "timestamp": obj.get("timestamp"),
                "view_count": obj.get("view_count"),
                "like_count": obj.get("like_count"),
                "comment_count": obj.get("comment_count"),
                "thumbnail": obj.get("thumbnail"),
                "webpage_url": obj.get("webpage_url") or url,
                "availability": obj.get("availability"),
                "subtitles": sorted((obj.get("subtitles") or {}).keys()),
                "automatic_captions": sorted((obj.get("automatic_captions") or {}).keys()),
            }
            return {
                "platform": "youtube",
                "ok": True,
                "source": "yt-dlp",
                "requested_url": url,
                "normalized": keep,
                "attempts": attempts,
            }
    except Exception as exc:
        attempts.append({
            "source": "yt-dlp",
            "ok": False,
            "valid_content": False,
            "status": None,
            "elapsed_seconds": round(time.time() - started, 3),
            "bytes": 0,
            "error": repr(exc),
        })

    endpoint = f"https://r.jina.ai/{url}"
    rec = fetch_text(endpoint, timeout=timeout)
    valid = rec["ok"] and len(rec["text"].strip()) > 100 and not looks_blocked(rec["text"])
    attempts.append(attempt_meta("jina", endpoint, rec, valid))
    if valid:
        return {
            "platform": "youtube",
            "ok": True,
            "source": "jina",
            "requested_url": url,
            "normalized": {"text": rec["text"], "url": url},
            "attempts": attempts,
        }
    return {"platform": "youtube", "ok": False, "source": None, "requested_url": url, "attempts": attempts}


def read_generic(url, timeout, platform):
    endpoint = f"https://r.jina.ai/{url}"
    rec = fetch_text(endpoint, timeout=timeout)
    valid = rec["ok"] and len(rec["text"].strip()) > 80 and not looks_blocked(rec["text"])
    return {
        "platform": platform,
        "ok": valid,
        "source": "jina" if valid else None,
        "requested_url": url,
        "normalized": {"text": rec["text"], "url": url} if valid else None,
        "attempts": [attempt_meta("jina", endpoint, rec, valid)],
    }


def read_one(url, timeout):
    platform = platform_for(url)
    started = time.time()
    if platform == "x":
        out = read_x(url, timeout)
    elif platform == "reddit":
        out = read_reddit(url, timeout)
    elif platform == "youtube":
        out = read_youtube(url, timeout)
    else:
        out = read_generic(url, timeout, platform)
    out["elapsed_seconds"] = round(time.time() - started, 3)
    out["fetched_at"] = now_iso()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("job_file")
    ap.add_argument("--output", default="social_reach_output")
    args = ap.parse_args()

    job = json.loads(Path(args.job_file).read_text(encoding="utf-8"))
    urls = job.get("urls") or []
    if not isinstance(urls, list) or not urls:
        raise SystemExit("job.urls must be a non-empty array")
    if len(urls) > 100:
        raise SystemExit("max 100 URLs per job")

    timeout = int(job.get("timeout_seconds", 20))
    workers = max(1, min(int(job.get("max_workers", min(8, len(urls)))), 16))
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)

    started_at = now_iso()
    started = time.time()
    results = [None] * len(urls)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(read_one, url, timeout): i for i, url in enumerate(urls)}
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                results[i] = fut.result()
            except Exception as exc:
                results[i] = {
                    "platform": platform_for(urls[i]),
                    "ok": False,
                    "source": None,
                    "requested_url": urls[i],
                    "error": repr(exc),
                    "fetched_at": now_iso(),
                }
            print(json.dumps({
                "url": urls[i],
                "platform": results[i].get("platform"),
                "ok": results[i].get("ok"),
                "source": results[i].get("source"),
                "elapsed_seconds": results[i].get("elapsed_seconds"),
            }, ensure_ascii=False), flush=True)

    manifest = {
        "job_name": job.get("name") or Path(args.job_file).stem,
        "engine": "runner-5-social-reach",
        "strategy": "fast-public-fallbacks-first; agent-reach-optional-breadth-layer",
        "started_at": started_at,
        "finished_at": now_iso(),
        "wall_seconds": round(time.time() - started, 3),
        "url_count": len(urls),
        "ok_count": sum(1 for r in results if r and r.get("ok")),
        "failed_count": sum(1 for r in results if not r or not r.get("ok")),
        "results": results,
    }
    (output / "result.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in ("url_count", "ok_count", "failed_count", "wall_seconds")}, ensure_ascii=False))
    return 0 if manifest["failed_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
