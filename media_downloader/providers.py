from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .models import MediaCandidate, ResolutionError
from .pipeline import ResolutionStrategy

PUBLIC_AGENT = "Media-Downloader-Tool/1.0 (public-media resolution)"
REDDIT_GIF_HOSTS = {"i.redd.it", "preview.redd.it"}


def host_matches(url: str, domains: tuple[str, ...]) -> bool:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def reddit_gif(url: str, title: str = "reddit-gif") -> MediaCandidate | None:
    normalized = html.unescape(url).replace(r"\/", "/").replace(r"\u0026", "&")
    parsed = urlparse(normalized)
    if parsed.netloc.lower().removeprefix("www.") not in REDDIT_GIF_HOSTS or ".gif" not in parsed.path.lower():
        return None
    return MediaCandidate(title=title, kind="gif", source_url=normalized, thumbnail_url=normalized,
                          resolution="Original GIF", direct=True)


@dataclass(frozen=True)
class YtDlpStrategy:
    root: Path
    name: str = "yt-dlp"

    def resolve(self, url: str) -> MediaCandidate:
        try:
            from yt_dlp import YoutubeDL
            with YoutubeDL({"noplaylist": True, "no_warnings": True, "quiet": True,
                            "format": "best[ext=mp4]"}) as downloader:
                info = downloader.extract_info(url, download=False)
        except Exception as exc:
            raise ResolutionError(str(exc) or "The source could not be processed.") from exc
        width, height = info.get("width"), info.get("height")
        return MediaCandidate(title=str(info.get("title") or "download"), kind="video",
                              source_url=str(info.get("url") or ""), thumbnail_url=str(info.get("thumbnail") or ""),
                              duration_seconds=float(info.get("duration") or 0),
                              duration_label=str(info.get("duration_string") or ""),
                              resolution=f"{width}×{height}" if width and height else "Best available resolution",
                              headers=dict(info.get("http_headers") or {}))


@dataclass(frozen=True)
class DirectRedditGifStrategy:
    name: str = "direct-reddit-gif"

    def resolve(self, url: str) -> MediaCandidate:
        candidate = reddit_gif(url)
        if not candidate:
            raise ResolutionError("The link is not a direct Reddit GIF.")
        return candidate


@dataclass(frozen=True)
class RedditJsonGifStrategy:
    endpoint_template: str
    name: str

    def resolve(self, url: str) -> MediaCandidate:
        match = re.search(r"/comments/([a-z0-9]+)", urlparse(url).path, re.I)
        if not match:
            raise ResolutionError("The link does not contain a Reddit post ID.")
        request = Request(self.endpoint_template.format(post_id=match.group(1)), headers={"User-Agent": PUBLIC_AGENT})
        try:
            with urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
            post = payload[0]["data"]["children"][0]["data"]
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise ResolutionError(f"Public metadata was unavailable: {exc}") from exc
        title = str(post.get("title") or "reddit-gif")
        candidates: list[str] = []
        metadata = post.get("media_metadata") or {}
        if isinstance(metadata, dict):
            for item in metadata.values():
                source = item.get("s") if isinstance(item, dict) else None
                if isinstance(source, dict) and isinstance(source.get("gif"), str):
                    candidates.append(source["gif"])
        preview = post.get("preview") or {}
        if isinstance(preview, dict):
            for image in preview.get("images") or []:
                source = image.get("source") if isinstance(image, dict) else None
                if isinstance(source, dict) and isinstance(source.get("url"), str):
                    candidates.append(source["url"])
        candidates.extend(value for value in (post.get("url_overridden_by_dest"), post.get("url")) if isinstance(value, str))
        for item in candidates:
            if candidate := reddit_gif(item, title):
                return candidate
        raise ResolutionError("Public metadata did not contain a hosted GIF.")


@dataclass(frozen=True)
class RedditPageGifStrategy:
    name: str = "reddit-page-metadata"

    def resolve(self, url: str) -> MediaCandidate:
        request = Request(url, headers={"User-Agent": PUBLIC_AGENT})
        try:
            with urlopen(request, timeout=20) as response:
                page = response.read().decode("utf-8", "replace")
        except OSError as exc:
            raise ResolutionError(f"Public post page was unavailable: {exc}") from exc
        candidates = re.findall(r'https?(?:://|:\\/\\/)[^"\'<>\s]+?\.gif(?:[^"\'<>\s]*)?', page, flags=re.I)
        for item in candidates:
            if candidate := reddit_gif(item):
                return candidate
        raise ResolutionError("Public post-page metadata did not contain a hosted GIF.")


@dataclass(frozen=True)
class YtDlpProvider:
    name: str
    domains: tuple[str, ...]
    root: Path

    def matches(self, url: str) -> bool:
        return host_matches(url, self.domains)

    def strategies(self, url: str) -> tuple[ResolutionStrategy, ...]:
        return (YtDlpStrategy(self.root),)


@dataclass(frozen=True)
class RedditProvider:
    root: Path
    name: str = "Reddit"
    domains: tuple[str, ...] = ("reddit.com", "redd.it", "i.redd.it", "preview.redd.it", "redditmedia.com")

    def matches(self, url: str) -> bool:
        return host_matches(url, self.domains)

    def strategies(self, url: str) -> tuple[ResolutionStrategy, ...]:
        return (
            DirectRedditGifStrategy(),
            YtDlpStrategy(self.root),
            RedditJsonGifStrategy("https://www.reddit.com/comments/{post_id}.json?raw_json=1", "reddit-json"),
            RedditJsonGifStrategy("https://old.reddit.com/comments/{post_id}.json?raw_json=1", "old-reddit-json"),
            RedditPageGifStrategy(),
        )
