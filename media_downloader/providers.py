from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

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


TIKTOK_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class TikTokWebStrategy:
    name: str = "tiktok-web"

    def resolve(self, url: str) -> MediaCandidate:
        cj = CookieJar()
        opener = build_opener(HTTPCookieProcessor(cj))
        req = Request(url, headers={
            "User-Agent": TIKTOK_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
        })
        try:
            with opener.open(req, timeout=20) as response:
                page = response.read().decode("utf-8", "replace")
        except OSError as exc:
            raise ResolutionError(f"TikTok page request failed: {exc}") from exc

        # Check __UNIVERSAL_DATA_FOR_REHYDRATION__
        match = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', page, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
                detail = data.get("__DEFAULT_SCOPE__", {}).get("webapp.video-detail", {})
                item = detail.get("itemInfo", {}).get("itemStruct", {})
                video = item.get("video", {})
                play_addr = video.get("playAddr") or video.get("downloadAddr")
                if play_addr:
                    cookie_header = "; ".join(f"{c.name}={c.value}" for c in cj)
                    stream_headers = {
                        "User-Agent": TIKTOK_AGENT,
                        "Referer": "https://www.tiktok.com/",
                    }
                    if cookie_header:
                        stream_headers["Cookie"] = cookie_header
                    duration = float(video.get("duration") or 0)
                    duration_str = f"{int(duration // 60)}:{int(duration % 60):02d}" if duration else ""
                    return MediaCandidate(
                        title=str(item.get("desc") or "tiktok-video"),
                        kind="video",
                        source_url=play_addr,
                        thumbnail_url=str(video.get("cover") or video.get("originCover") or ""),
                        duration_seconds=duration,
                        duration_label=duration_str,
                        resolution=str(video.get("ratio") or video.get("definition") or "Best available resolution"),
                        headers=stream_headers,
                        direct=True,
                    )
            except Exception:
                pass

        # Check SIGI_STATE fallback
        sigi = re.search(r'<script id="SIGI_STATE"[^>]*>(.*?)</script>', page, re.DOTALL)
        if sigi:
            try:
                data = json.loads(sigi.group(1))
                items = data.get("ItemModule", {})
                if items:
                    item = next(iter(items.values()))
                    video = item.get("video", {})
                    play_addr = video.get("playAddr") or video.get("downloadAddr")
                    if play_addr:
                        cookie_header = "; ".join(f"{c.name}={c.value}" for c in cj)
                        stream_headers = {
                            "User-Agent": TIKTOK_AGENT,
                            "Referer": "https://www.tiktok.com/",
                        }
                        if cookie_header:
                            stream_headers["Cookie"] = cookie_header
                        duration = float(video.get("duration") or 0)
                        return MediaCandidate(
                            title=str(item.get("desc") or "tiktok-video"),
                            kind="video",
                            source_url=play_addr,
                            thumbnail_url=str(video.get("cover") or ""),
                            duration_seconds=duration,
                            duration_label=f"{int(duration // 60)}:{int(duration % 60):02d}" if duration else "",
                            resolution=str(video.get("ratio") or "Best available resolution"),
                            headers=stream_headers,
                            direct=True,
                        )
            except Exception:
                pass

        raise ResolutionError("No video streams found in TikTok page metadata.")


@dataclass(frozen=True)
class TikTokPublicApiStrategy:
    name: str = "tiktok-api"

    def resolve(self, url: str) -> MediaCandidate:
        api_url = f"https://www.tikwm.com/api/?url={quote(url, safe='')}"
        req = Request(api_url, headers={"User-Agent": TIKTOK_AGENT})
        try:
            with urlopen(req, timeout=15) as res:
                payload = json.loads(res.read().decode("utf-8", "replace"))
        except OSError as exc:
            raise ResolutionError(f"TikTok public API unreachable: {exc}") from exc

        if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
            raise ResolutionError(f"TikTok public API error: {payload.get('msg', 'Unknown')}")

        data = payload["data"]
        play_url = data.get("play") or data.get("wmplay")
        if not play_url:
            raise ResolutionError("TikTok public API did not provide a media URL.")

        duration = float(data.get("duration") or 0)
        return MediaCandidate(
            title=str(data.get("title") or "tiktok-video"),
            kind="video",
            source_url=play_url,
            thumbnail_url=str(data.get("cover") or data.get("origin_cover") or ""),
            duration_seconds=duration,
            duration_label=f"{int(duration // 60)}:{int(duration % 60):02d}" if duration else "",
            resolution="Best available resolution",
            headers={"User-Agent": TIKTOK_AGENT, "Referer": "https://www.tiktok.com/"},
            direct=True,
        )


@dataclass(frozen=True)
class TikTokProvider:
    root: Path
    name: str = "TikTok"
    domains: tuple[str, ...] = ("tiktok.com",)

    def matches(self, url: str) -> bool:
        return host_matches(url, self.domains)

    def strategies(self, url: str) -> tuple[ResolutionStrategy, ...]:
        return (
            TikTokWebStrategy(),
            TikTokPublicApiStrategy(),
            YtDlpStrategy(self.root),
        )

