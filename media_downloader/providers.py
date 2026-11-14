from __future__ import annotations

import html
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

from .models import MediaCandidate, ResolutionError
from .pipeline import ResolutionStrategy

PUBLIC_AGENT = "Media-Downloader-Tool/1.0 (public-media resolution)"
REDDIT_GIF_HOSTS = {"i.redd.it", "preview.redd.it"}

# Prefer MP4/M4A so the merged result is broadly playable, but retain yt-dlp's
# normal best-video/best-audio fallback for videos where YouTube exposes other
# containers only.  A single ``best[ext=mp4]`` stream is no longer available on
# many otherwise public YouTube videos.
YTDLP_VIDEO_FORMAT = "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"


def yt_dlp_options() -> dict[str, object]:
    """Shared safe defaults for public, no-login yt-dlp extraction."""
    return {
        "noplaylist": True,
        "no_warnings": True,
        "quiet": True,
        "windows_hide": True,
        "format": YTDLP_VIDEO_FORMAT,
        # Deno is yt-dlp's default; Node is an additional supported fallback
        # when it is already installed. Missing runtimes are ignored by yt-dlp.
        "js_runtimes": {"deno": {}, "node": {}},
    }


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
            with YoutubeDL(yt_dlp_options()) as downloader:
                info = downloader.extract_info(url, download=False)
        except Exception as exc:
            raise ResolutionError(str(exc) or "The source could not be processed.") from exc
        width, height = info.get("width"), info.get("height")
        # Split video/audio selections have URLs in requested_formats rather
        # than one playable URL. In that case the UI uses the thumbnail for the
        # preview, while the download pass merges both streams with FFmpeg.
        source_url = str(info.get("url") or "")
        return MediaCandidate(title=str(info.get("title") or "download"), kind="video",
                              source_url=source_url, thumbnail_url=str(info.get("thumbnail") or ""),
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


@dataclass
class _PageMedia:
    url: str
    source: str
    prominence: float
    order: int
    width: int = 0
    height: int = 0
    duration: float = 0
    size: int = 0
    thumbnail: str = ""

    @property
    def score(self) -> float:
        pixels = self.width * self.height
        resolution_score = math.log2(pixels + 1) * 12 if pixels else 0
        duration_score = min(self.duration, 14_400) / 240 if self.duration else 0
        size_score = math.log2(self.size + 1) * 2 if self.size else 0
        return self.prominence + resolution_score + duration_score + size_score - min(self.order, 200) * .25


def _number(value: object) -> float:
    try:
        return float(str(value or "").strip())
    except (TypeError, ValueError):
        return 0


def _duration_seconds(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    match = re.fullmatch(r"P(?:(\d+(?:\.\d+)?)D)?T?(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?", text, re.I)
    if not match:
        return 0
    days, hours, minutes, seconds = (_number(part) for part in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _content_size(value: object) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip().lower().replace(",", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(b|kb|kib|mb|mib|gb|gib)?", text)
    if not match:
        return 0
    multiplier = {"kb": 1000, "kib": 1024, "mb": 1_000_000, "mib": 1_048_576,
                  "gb": 1_000_000_000, "gib": 1_073_741_824}.get(match.group(2) or "b", 1)
    return int(float(match.group(1)) * multiplier)


class _MediaPageParser(HTMLParser):
    def __init__(self, page_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.candidates: list[_PageMedia] = []
        self.meta: dict[str, str] = {}
        self.json_ld: list[str] = []
        self.title = ""
        self._in_title = False
        self._in_json_ld = False
        self._script_parts: list[str] = []
        self._video_depth = 0
        self._order = 0

    def add(self, raw_url: object, source: str, prominence: float, attrs: dict[str, str] | None = None) -> None:
        value = html.unescape(str(raw_url or "").strip()).replace(r"\/", "/")
        if not value or value.startswith(("data:", "blob:", "javascript:")):
            return
        resolved = urljoin(self.page_url, value)
        if urlparse(resolved).scheme not in {"http", "https"}:
            return
        values = attrs or {}
        self.candidates.append(_PageMedia(
            resolved, source, prominence, self._order,
            int(_number(values.get("width"))), int(_number(values.get("height"))),
            _duration_seconds(values.get("duration")), _content_size(values.get("size")),
            urljoin(self.page_url, values.get("poster", "")) if values.get("poster") else "",
        ))
        self._order += 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        tag = tag.lower()
        if tag == "video":
            self._video_depth += 1
            self.add(values.get("src"), "video", 1000, values)
        elif tag == "source":
            self.add(values.get("src"), "source", 950 if self._video_depth else 700, values)
        elif tag == "meta":
            key = (values.get("property") or values.get("name") or values.get("itemprop") or "").lower()
            content = values.get("content", "")
            if key and content:
                self.meta[key] = content
                if key in {"og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream"}:
                    self.add(content, "open-graph", 900)
        elif tag == "script" and "ld+json" in values.get("type", "").lower():
            self._in_json_ld = True
            self._script_parts = []
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "video" and self._video_depth:
            self._video_depth -= 1
        elif tag == "script" and self._in_json_ld:
            self.json_ld.append("".join(self._script_parts))
            self._in_json_ld = False
            self._script_parts = []
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_json_ld:
            self._script_parts.append(data)
        elif self._in_title:
            self.title += data


def _json_ld_media(parser: _MediaPageParser, value: object) -> None:
    if isinstance(value, list):
        for item in value:
            _json_ld_media(parser, item)
        return
    if not isinstance(value, dict):
        return
    kind = value.get("@type")
    types = {str(item).lower() for item in kind} if isinstance(kind, list) else {str(kind).lower()}
    is_media = bool(types & {"videoobject", "audioobject", "mediaobject"})
    if is_media:
        attrs = {
            "width": str(value.get("width") or ""), "height": str(value.get("height") or ""),
            "duration": str(value.get("duration") or ""), "size": str(value.get("contentSize") or ""),
            "poster": str(value.get("thumbnailUrl") or ""),
        }
        for key in ("contentUrl", "embedUrl"):
            raw = value.get(key)
            if isinstance(raw, list):
                for item in raw:
                    parser.add(item, "json-ld", 850 if key == "contentUrl" else 650, attrs)
            else:
                parser.add(raw, "json-ld", 850 if key == "contentUrl" else 650, attrs)
    for child in value.values():
        if isinstance(child, (dict, list)):
            _json_ld_media(parser, child)


def _probe_page_media(item: _PageMedia, referer: str) -> _PageMedia | None:
    request = Request(item.url, method="HEAD", headers={"User-Agent": PUBLIC_AGENT, "Referer": referer})
    try:
        with urlopen(request, timeout=8) as response:
            content_type = response.headers.get("Content-Type", "").lower()
            if content_type.startswith(("text/html", "application/xhtml")):
                return None
            item.url = response.geturl()
            item.size = item.size or _content_size(response.headers.get("Content-Length"))
    except OSError:
        # Many CDNs reject HEAD while allowing GET, so an unprobed candidate is
        # still eligible rather than being treated as definitively unavailable.
        pass
    return item


@dataclass(frozen=True)
class GenericPageStrategy:
    name: str = "generic-page-candidates"

    def resolve(self, url: str) -> MediaCandidate:
        request = Request(url, headers={"User-Agent": PUBLIC_AGENT, "Accept": "text/html,application/xhtml+xml"})
        try:
            with urlopen(request, timeout=25) as response:
                content_type = response.headers.get("Content-Type", "").lower()
                body = response.read(5_000_000)
                final_url = response.geturl()
        except OSError as exc:
            raise ResolutionError(f"The public page could not be read: {exc}") from exc

        direct_suffix = Path(urlparse(final_url).path).suffix.lower()
        direct_types = ("video/", "audio/", "application/vnd.apple.mpegurl", "application/x-mpegurl", "application/dash+xml")
        if content_type.startswith(direct_types) or direct_suffix in {".mp4", ".webm", ".mov", ".m4v", ".m3u8", ".mpd"}:
            return MediaCandidate(title=Path(urlparse(final_url).path).stem or "download", kind="video",
                                  source_url=final_url, resolution="Direct media", direct=True)
        text = body.decode("utf-8", "replace")
        parser = _MediaPageParser(final_url)
        try:
            parser.feed(text)
        except Exception as exc:
            raise ResolutionError(f"The page markup could not be inspected: {exc}") from exc

        for raw_json in parser.json_ld:
            try:
                _json_ld_media(parser, json.loads(raw_json))
            except (TypeError, ValueError):
                continue

        manifest_pattern = r'''(?i)(?:https?:)?//[^\s"'<>\\]+?\.(?:m3u8|mpd)(?:\?[^\s"'<>\\]*)?|(?:/|\.\.?/)[^\s"'<>\\]+?\.(?:m3u8|mpd)(?:\?[^\s"'<>\\]*)?'''
        for match in re.finditer(manifest_pattern, html.unescape(text).replace(r"\/", "/")):
            parser.add(match.group(0), "manifest", 750)

        og_width = int(_number(parser.meta.get("og:video:width")))
        og_height = int(_number(parser.meta.get("og:video:height")))
        og_duration = _duration_seconds(parser.meta.get("video:duration") or parser.meta.get("og:video:duration"))
        for item in parser.candidates:
            if item.source == "open-graph":
                item.width = item.width or og_width
                item.height = item.height or og_height
                item.duration = item.duration or og_duration

        deduplicated: dict[str, _PageMedia] = {}
        for item in parser.candidates:
            current = deduplicated.get(item.url)
            if current is None or item.score > current.score:
                deduplicated[item.url] = item
        if not deduplicated:
            raise ResolutionError("No public <video>, <source>, Open Graph, JSON-LD, HLS, or DASH media candidates were found.")

        candidates = list(deduplicated.values())[:32]
        with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as executor:
            probed = list(executor.map(lambda item: _probe_page_media(item, final_url), candidates))
        ranked = sorted((item for item in probed if item is not None), key=lambda item: item.score, reverse=True)
        if not ranked:
            raise ResolutionError("The discovered media candidates resolved to HTML pages rather than downloadable media.")
        best = ranked[0]
        title = (parser.meta.get("og:title") or parser.title or Path(urlparse(url).path).stem or "download").strip()
        thumbnail = best.thumbnail or parser.meta.get("og:image", "")
        resolution = f"{best.width}×{best.height}" if best.width and best.height else "Best ranked page candidate"
        return MediaCandidate(
            title=title, kind="video", source_url=best.url, thumbnail_url=thumbnail,
            duration_seconds=best.duration,
            duration_label=(f"{int(best.duration // 60)}:{int(best.duration % 60):02d}" if best.duration else ""),
            resolution=resolution,
            headers={"User-Agent": PUBLIC_AGENT, "Referer": final_url}, direct=True,
        )


@dataclass(frozen=True)
class GenericProvider:
    root: Path
    name: str = "Generic"

    def matches(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)

    def strategies(self, url: str) -> tuple[ResolutionStrategy, ...]:
        return (YtDlpStrategy(self.root), GenericPageStrategy())


REDDIT_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)
REDDIT_HEADERS = {
    "User-Agent": REDDIT_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.reddit.com/",
}


@dataclass(frozen=True)
class RedditShredditStrategy:
    name: str = "reddit-shreddit"

    def resolve(self, url: str) -> MediaCandidate:
        req = Request(url, headers=REDDIT_HEADERS)
        try:
            with urlopen(req, timeout=20) as res:
                page = res.read().decode("utf-8", "replace")
        except OSError as exc:
            raise ResolutionError(f"Reddit page request failed: {exc}") from exc

        post_match = re.search(r'<shreddit-post\s+([^>]+)>', page)
        player_match = re.search(r'<shreddit-player\s+([^>]+)>', page)

        if not post_match and not player_match:
            raise ResolutionError("No Shreddit metadata found on page.")

        post_attrs = dict(re.findall(r'([a-z0-9_-]+)="([^"]*)"', post_match.group(1))) if post_match else {}
        player_attrs = dict(re.findall(r'([a-z0-9_-]+)="([^"]*)"', player_match.group(1))) if player_match else {}

        raw_title = post_attrs.get("post-title") or player_attrs.get("post-title") or "reddit-media"
        title = html.unescape(raw_title)

        poster = html.unescape(player_attrs.get("poster") or player_attrs.get("preview") or "")
        content_href = html.unescape(post_attrs.get("content-href") or "")
        post_type = post_attrs.get("post-type", "")

        # 1. Native GIF check
        if post_type == "gif" or ".gif" in content_href.lower():
            if content_href and ".gif" in content_href.lower():
                return MediaCandidate(
                    title=title,
                    kind="gif",
                    source_url=content_href,
                    thumbnail_url=poster or content_href,
                    resolution="Original GIF",
                    headers=dict(REDDIT_HEADERS),
                    direct=True,
                )

        # 2. Check packaged-media-json for pre-multiplexed progressive MP4
        if raw_pkg := player_attrs.get("packaged-media-json"):
            try:
                pkg_data = json.loads(html.unescape(raw_pkg))
                duration = float(pkg_data.get("playbackMp4s", {}).get("duration") or 0)
                permutations = pkg_data.get("playbackMp4s", {}).get("permutations", [])
                if permutations:
                    best = max(permutations, key=lambda p: int(p.get("source", {}).get("dimensions", {}).get("height", 0)))
                    src_url = html.unescape(best["source"]["url"])
                    dims = best["source"].get("dimensions", {})
                    w, h = dims.get("width"), dims.get("height")
                    res_label = f"{w}×{h}" if w and h else "Best available resolution"
                    return MediaCandidate(
                        title=title,
                        kind="video",
                        source_url=src_url,
                        thumbnail_url=poster,
                        duration_seconds=duration,
                        duration_label=f"{int(duration // 60)}:{int(duration % 60):02d}" if duration else "",
                        resolution=res_label,
                        headers=dict(REDDIT_HEADERS),
                        direct=True,
                    )
            except Exception:
                pass

        # 3. Check player src (HLS, DASH, or MP4)
        if raw_src := player_attrs.get("src"):
            src_url = html.unescape(raw_src)
            return MediaCandidate(
                title=title,
                kind="video",
                source_url=src_url,
                thumbnail_url=poster,
                resolution="Best available resolution",
                headers=dict(REDDIT_HEADERS),
                direct=True,
            )

        # 4. Check v.redd.it content_href
        if "v.redd.it" in content_href:
            hls_url = f"{content_href.rstrip('/')}/HLSPlaylist.m3u8"
            return MediaCandidate(
                title=title,
                kind="video",
                source_url=hls_url,
                thumbnail_url=poster,
                resolution="Best available resolution",
                headers=dict(REDDIT_HEADERS),
                direct=True,
            )

        raise ResolutionError("No compatible media streams in Shreddit metadata.")


@dataclass(frozen=True)
class RedditEmbedStrategy:
    name: str = "reddit-embed"

    def resolve(self, url: str) -> MediaCandidate:
        match = re.search(r"/comments/([a-z0-9]+)", urlparse(url).path, re.I)
        if not match:
            req = Request(url, headers=REDDIT_HEADERS)
            try:
                with urlopen(req, timeout=10) as res:
                    final_path = urlparse(res.geturl()).path
                    match = re.search(r"/comments/([a-z0-9]+)", final_path, re.I)
            except OSError:
                pass
        if not match:
            raise ResolutionError("Could not determine Reddit post ID for embed.")

        embed_url = f"https://www.reddit.com/comments/{match.group(1)}.embed"
        req = Request(embed_url, headers=REDDIT_HEADERS)
        try:
            with urlopen(req, timeout=15) as res:
                page = res.read().decode("utf-8", "replace")
        except OSError as exc:
            raise ResolutionError(f"Reddit embed request failed: {exc}") from exc

        gif_matches = re.findall(r'https?://[^\s"\'<>]+\.gif[^\s"\'<>]*', page, re.I)
        for g in gif_matches:
            unescaped = html.unescape(g).rstrip(r"\ ")
            if "i.redd.it" in unescaped or "preview.redd.it" in unescaped:
                return MediaCandidate(
                    title=f"reddit-gif-{match.group(1)}",
                    kind="gif",
                    source_url=unescaped,
                    thumbnail_url=unescaped,
                    resolution="Original GIF",
                    headers=dict(REDDIT_HEADERS),
                    direct=True,
                )

        v_matches = re.findall(r'https?://(?:v\.redd\.it|packaged-media\.redd\.it)[^\s"\'<>]+', page)
        for v in v_matches:
            unescaped = html.unescape(v).rstrip(r"\ ")
            return MediaCandidate(
                title=f"reddit-video-{match.group(1)}",
                kind="video",
                source_url=unescaped,
                resolution="Best available resolution",
                headers=dict(REDDIT_HEADERS),
                direct=True,
            )

        raise ResolutionError("No media found in Reddit embed page.")


@dataclass(frozen=True)
class RedditProvider:
    root: Path
    name: str = "Reddit"
    domains: tuple[str, ...] = ("reddit.com", "redd.it", "i.redd.it", "preview.redd.it", "redditmedia.com", "v.redd.it")

    def matches(self, url: str) -> bool:
        return host_matches(url, self.domains)

    def strategies(self, url: str) -> tuple[ResolutionStrategy, ...]:
        return (
            DirectRedditGifStrategy(),
            RedditShredditStrategy(),
            RedditEmbedStrategy(),
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

