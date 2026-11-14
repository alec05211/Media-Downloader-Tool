"""Local downloader for public media the user is permitted to save."""
from __future__ import annotations
import json, math, mimetypes, os, re, secrets, shutil, string, subprocess, sys, threading, time, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import Tk, filedialog
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from media_downloader.diagnostics import record_download_failure, record_resolution
from media_downloader.models import MediaCandidate
from media_downloader.pipeline import ProviderAdapter, resolve
from media_downloader.providers import GenericProvider, RedditProvider, TikTokProvider, YtDlpProvider, yt_dlp_options

# In a PyInstaller one-file build, the program files are unpacked to a temporary
# directory. Keep user data outside that directory so it survives app restarts.
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
APP_DATA = Path.home() / "AppData" / "Local" / "Media Downloader"
APP_DATA.mkdir(parents=True, exist_ok=True)
# Purge legacy appdata downloads directory if it exists
downloads_dir = APP_DATA / "downloads"
if downloads_dir.exists():
    try: shutil.rmtree(downloads_dir, ignore_errors=True)
    except OSError: pass

LAST_SAVE_FOLDER = APP_DATA / ".last_save_folder"
FOLDER_HISTORY = APP_DATA / "folder_history.json"
INSTANCE_FILE = APP_DATA / "instance.json"
APP_PORT = 38471
FOLDER_LOCK = threading.Lock()

PREVIEWS: dict[str, dict[str, object]] = {}
DIRECT_MEDIA: dict[str, str] = {}
RESOLVED_CANDIDATES: dict[str, MediaCandidate] = {}
RESOLUTION_STRATEGIES: dict[str, str] = {}
PROVIDERS: tuple[ProviderAdapter, ...] = (
    YtDlpProvider("YouTube", ("youtube.com", "youtu.be"), ROOT),
    TikTokProvider(ROOT),
    YtDlpProvider("Instagram", ("instagram.com",), ROOT),
    YtDlpProvider("X", ("x.com", "twitter.com"), ROOT),
    RedditProvider(ROOT),
    GenericProvider(ROOT),
)
JOBS: list[dict[str,str]] = []; LOCK = threading.Lock()
EDITOR_TEMP = APP_DATA / "editor"
EDITOR_UPLOADS: dict[str, dict[str, object]] = {}
EDITOR_JOBS: dict[str, dict[str, object]] = {}
EDITOR_LOCK = threading.Lock()
EDITOR_EXTENSIONS = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"}
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

def run_hidden(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, creationflags=NO_WINDOW, **kwargs)

def protocol_launch_url(arguments: list[str] | None = None) -> str:
    """Extract a safe web URL from a media-downloader:// launch argument."""
    for argument in (sys.argv[1:] if arguments is None else arguments):
        try:
            parsed = urlsplit(argument)
        except ValueError:
            continue
        if parsed.scheme.lower() != "media-downloader":
            continue
        candidate = parse_qs(parsed.query).get("url", [""])[0].strip()
        try:
            web_url = urlsplit(candidate)
        except ValueError:
            continue
        if web_url.scheme.lower() in {"http", "https"} and web_url.netloc and len(candidate) <= 16384:
            return candidate
    return ""

def browser_address(base_address: str, launch_url: str = "") -> str:
    if not launch_url:
        return base_address
    return f"{base_address}/?{urlencode({'url': launch_url})}"

def running_instance_address(fixed_only: bool = False) -> str:
    """Return a live Media Downloader server address, ignoring stale state."""
    ports = [APP_PORT]
    if not fixed_only:
        try:
            state = json.loads(INSTANCE_FILE.read_text(encoding="utf-8"))
            port = int(state.get("port", 0))
            if 1 <= port <= 65535 and port not in ports:
                ports.append(port)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    for port in ports:
        address = f"http://127.0.0.1:{port}"
        try:
            with urlopen(f"{address}/api/app-status", timeout=0.75) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("app") == "media-downloader":
                return address
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return ""

def provider_for(url: str) -> ProviderAdapter | None:
    return next((provider for provider in PROVIDERS if provider.matches(url)), None)

def safe_name(value: str) -> str:
    return (re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .") or "download")[:120]

def is_restricted_folder(folder_path: Path | str) -> bool:
    try:
        resolved = Path(folder_path).resolve()
        user_downloads = (Path.home() / "Downloads").resolve()
        app_data = APP_DATA.resolve()
        if resolved == user_downloads: return True
        if resolved == app_data or app_data in resolved.parents: return True
    except Exception:
        pass
    return False

def load_folder_history() -> dict[str, dict[str, object]]:
    with FOLDER_LOCK:
        data: dict[str, dict[str, object]] = {}
        if FOLDER_HISTORY.exists():
            try:
                raw = json.loads(FOLDER_HISTORY.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data = raw
            except Exception:
                pass
        # Never retain restricted folders in history
        for k in list(data.keys()):
            if is_restricted_folder(k):
                del data[k]
        if not data:
            now = time.time()
            candidates: list[str] = []
            for p in [LAST_SAVE_FOLDER, ROOT / ".last_save_folder"]:
                try:
                    if p.exists():
                        f = Path(p.read_text(encoding="utf-8").strip())
                        if f.is_dir() and not is_restricted_folder(f):
                            candidates.append(str(f))
                except Exception:
                    pass
            seen = set()
            for i, c in enumerate(candidates):
                try:
                    norm = str(Path(c).resolve())
                except Exception:
                    norm = c
                if norm not in seen and Path(norm).is_dir() and not is_restricted_folder(norm):
                    seen.add(norm)
                    data[norm] = {"count": max(1, 5 - i), "last_used": now - (i * 60)}
        try:
            FOLDER_HISTORY.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass
        return data

def save_folder_history(data: dict[str, dict[str, object]]) -> None:
    with FOLDER_LOCK:
        try:
            FOLDER_HISTORY.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass

def record_folder_use(folder_path: str) -> None:
    if is_restricted_folder(folder_path):
        return
    try:
        norm = str(Path(folder_path).resolve())
    except Exception:
        norm = folder_path
    data = load_folder_history()
    now = time.time()
    if norm in data:
        data[norm]["count"] = int(data[norm].get("count", 0)) + 1
        data[norm]["last_used"] = now
    else:
        data[norm] = {"count": 1, "last_used": now}
    save_folder_history(data)
    try:
        LAST_SAVE_FOLDER.write_text(norm, encoding="utf-8")
    except OSError:
        pass

def remove_folder(folder_path: str) -> None:
    data = load_folder_history()
    to_delete = []
    for k in data:
        try:
            if Path(k).resolve() == Path(folder_path).resolve():
                to_delete.append(k)
        except Exception:
            if k == folder_path:
                to_delete.append(k)
    for k in to_delete:
        del data[k]
    save_folder_history(data)

def get_folders_summary() -> dict[str, list[dict[str, object]]]:
    data = load_folder_history()
    valid: list[dict[str, object]] = []
    for path_str, info in data.items():
        if is_restricted_folder(path_str):
            continue
        try:
            p = Path(path_str)
            if not p.is_dir():
                continue
        except Exception:
            continue
        valid.append({
            "path": path_str,
            "name": p.name or path_str,
            "count": int(info.get("count", 1)),
            "last_used": float(info.get("last_used", 0)),
        })
    frequent = sorted(valid, key=lambda x: (x["count"], x["last_used"]), reverse=True)
    recent = sorted(valid, key=lambda x: (x["last_used"], x["count"]), reverse=True)
    return {"frequent": frequent, "recent": recent}

def generate_unique_path(folder: Path, mode: str) -> Path:
    suffix = {"gif": ".gif", "audio": ".mp3"}.get(mode, ".mp4")
    for _ in range(500):
        name = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12)) + suffix
        target = folder / name
        if not target.exists():
            return target
    return folder / f"download_{int(time.time())}{suffix}"

def random_media_name(suffix: str = ".mp4") -> str:
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12)) + suffix

def choose_path(title: str, mode: str) -> str:
    suffix = {"gif": ".gif", "audio": ".mp3"}.get(mode, ".mp4")
    name = random_media_name(suffix)
    last_folder = Path.home()
    try:
        candidate_folder = Path(LAST_SAVE_FOLDER.read_text(encoding="utf-8").strip())
        if candidate_folder.is_dir() and not is_restricted_folder(candidate_folder):
            last_folder = candidate_folder
    except OSError:
        pass
    root = Tk(); root.withdraw(); root.attributes("-topmost", True)
    result = filedialog.asksaveasfilename(title="Save download as", initialdir=last_folder, initialfile=name,
        defaultextension=suffix, filetypes=([("GIF image","*.gif")] if mode == "gif" else [("MP3 audio","*.mp3")] if mode == "audio" else [("MP4 video","*.mp4")]))
    root.destroy()
    if not result:
        raise ValueError("Save cancelled.")
    chosen = Path(result)
    record_folder_use(str(chosen.parent))
    return str(chosen)

def choose_editor_path() -> str:
    name = random_media_name()
    last_folder = Path.home()
    try:
        candidate_folder = Path(LAST_SAVE_FOLDER.read_text(encoding="utf-8").strip())
        if candidate_folder.is_dir() and not is_restricted_folder(candidate_folder):
            last_folder = candidate_folder
    except OSError:
        pass
    root = Tk(); root.withdraw(); root.attributes("-topmost", True)
    result = filedialog.asksaveasfilename(
        title="Export edited video as",
        initialdir=last_folder,
        initialfile=name,
        defaultextension=".mp4",
        filetypes=[("MP4 video", "*.mp4")],
    )
    root.destroy()
    if not result:
        raise ValueError("Export cancelled.")
    chosen = Path(result)
    record_folder_use(str(chosen.parent))
    return str(chosen)

def editor_replace_paths(original_path: Path, final_path: Path, token: str | None = None) -> tuple[Path, Path]:
    temp_token = token or secrets.token_hex(6)
    temporary_path = final_path.parent / f".{final_path.stem}.{temp_token}.editing.mp4"
    return temporary_path, final_path

def editor_quick_save_path(folder: Path) -> Path:
    target_folder = folder.resolve()
    if not target_folder.is_dir() or is_restricted_folder(target_folder):
        raise ValueError("The selected quick-save folder is unavailable.")
    return generate_unique_path(target_folder, "video").resolve()

def editor_should_replace_source(save_mode: str, upload: dict[str, object]) -> bool:
    return save_mode == "replace" and bool(upload.get("original_path"))

def editor_crop_filter(x: float, y: float, width: float, height: float) -> str | None:
    values = (x, y, width, height)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Invalid crop selection.")
    if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1.000001 or y + height > 1.000001:
        raise ValueError("The crop selection must stay inside the video.")
    if x <= 0.000001 and y <= 0.000001 and width >= 0.999999 and height >= 0.999999:
        return None
    return (
        f"crop=w='max(2,trunc(iw*{width:.8f}/2)*2)'"
        f":h='max(2,trunc(ih*{height:.8f}/2)*2)'"
        f":x='trunc(iw*{x:.8f}/2)*2'"
        f":y='trunc(ih*{y:.8f}/2)*2'"
    )

def export_edited_video(job_id: str) -> None:
    with EDITOR_LOCK:
        job = EDITOR_JOBS[job_id]
        upload = EDITOR_UPLOADS.get(str(job["upload_id"]))
    try:
        if not upload:
            raise RuntimeError("The uploaded video is no longer available.")
        ffmpeg = ffmpeg_binary()
        if not ffmpeg:
            raise RuntimeError("FFmpeg is unavailable. Reinstall the app and try again.")
        start = float(job["start"])
        end = float(job["end"])
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise ValueError("The trim end must be after the trim start.")
        crop_filter = editor_crop_filter(
            float(job["crop_x"]), float(job["crop_y"]), float(job["crop_width"]), float(job["crop_height"])
        )
        source = str(upload.get("path") or upload.get("source_url") or "")
        if not source:
            raise RuntimeError("The editor source is no longer available.")
        command = [ffmpeg, "-y", "-ss", f"{start:.3f}"]
        source_headers = upload.get("headers") or {}
        if source_headers:
            header_block = "".join(f"{key}: {value}\r\n" for key, value in dict(source_headers).items())
            command.extend(["-headers", header_block])
        command.extend(["-i", source, "-t", f"{end - start:.3f}"])
        video_filters = [crop_filter] if crop_filter else []
        video_filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
        command.extend(["-vf", ",".join(video_filters)])
        command.extend([
            "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-pix_fmt", "yuv420p", str(job["path"]),
        ])
        result = run_hidden(command, capture_output=True, text=True, timeout=60 * 60 * 4)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "FFmpeg export failed.")[-4000:])
        if bool(job.get("replace_source")):
            original_path = Path(str(upload["original_path"]))
            final_path = Path(str(job["final_path"]))
            os.replace(str(job["path"]), final_path)
            if original_path != final_path and original_path.exists():
                original_path.unlink()
            job["path"] = str(final_path)
            job["filename"] = final_path.name
            upload["path"] = str(final_path)
            upload["original_path"] = str(final_path)
        with EDITOR_LOCK:
            job["status"] = "complete"
    except Exception as exc:
        if bool(job.get("replace_source")):
            try:
                Path(str(job["path"])).unlink(missing_ok=True)
            except OSError:
                pass
        with EDITOR_LOCK:
            job["status"] = "failed"
            job["error"] = str(exc)

def ffmpeg_binary() -> str | None:
    if installed := shutil.which("ffmpeg"): return installed
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError): return None

def parse_video_frame_rate(ffmpeg_output: str) -> float:
    video_line = next((line for line in ffmpeg_output.splitlines() if "Video:" in line), "")
    match = re.search(r"(?:,|\s)(\d+(?:\.\d+)?)\s+fps(?:,|\s)", video_line)
    if not match:
        return 30.0
    frame_rate = float(match.group(1))
    return frame_rate if 1 <= frame_rate <= 240 else 30.0

def probe_video_frame_rate(path: Path | str) -> float:
    ffmpeg = ffmpeg_binary()
    if not ffmpeg:
        return 30.0
    try:
        result = run_hidden([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, text=True, timeout=20)
        return parse_video_frame_rate(result.stderr or result.stdout)
    except (OSError, subprocess.TimeoutExpired):
        return 30.0

def clipboard_text() -> str:
    """Read the desktop clipboard so paste works even when browser permission is denied."""
    root = Tk()
    root.withdraw()
    try:
        return root.clipboard_get()
    except Exception:
        return ""
    finally:
        root.destroy()

def classify_failure(text: str) -> tuple[str, str]:
    value = text.lower()
    if "no module named 'yt_dlp'" in value: return "DEP001", "The yt-dlp engine is unavailable to this Python installation. Run start.bat to install dependencies."
    if "encoder not found" in value or "conversion failed" in value: return "GIF002", "FFmpeg could not encode the downloaded video as GIF. The source may use an unsupported stream or the conversion process was interrupted."
    if "ffmpeg" in value: return "GIF001", "GIF conversion could not start because the FFmpeg runtime is missing or failed to initialize. Restart with start.bat; if it persists, reinstall dependencies."
    if "private video" in value or "login" in value or "sign in" in value: return "AUTH001", "The source requires sign-in, is private, or restricts anonymous downloads. This app does not use account credentials."
    if "age" in value and "restrict" in value: return "AUTH002", "The source is age-restricted and requires an authenticated session."
    if "geo" in value or "not available in your country" in value: return "SRC001", "The source has a geographic availability restriction."
    if "403" in value and ("reddit" in value or "preview.redd.it" in value or "i.redd.it" in value): return "SRC003", "Reddit blocked this public-media request. Try opening the post in a browser and saving its image directly."
    if "requested format is not available" in value or "no video formats" in value: return "FMT001", "No compatible anonymous audio/video streams were exposed by the source. The video may now require sign-in, proof-of-origin attestation, or a newer yt-dlp release."
    if "javascript runtime" in value or "challenge solving failed" in value or "yt-dlp-ejs" in value: return "DEP002", "YouTube's JavaScript challenge could not be solved. Rebuild or reinstall the app to refresh yt-dlp and its challenge scripts, and install a supported Deno or Node runtime if the diagnostic requests one."
    if "unable to download" in value or "timed out" in value or "connection" in value: return "NET001", "The source could not be reached or stopped responding. Check your connection and retry."
    if "permission denied" in value or "access is denied" in value: return "FS001", "Windows denied permission to write to the selected destination. Choose another folder or adjust its permissions."
    if "video unavailable" in value or "this video is not available" in value or "does not exist" in value: return "SRC002", "The source video is unavailable, removed, or the link is no longer valid."
    return "DL999", "The downloader returned an unclassified error. Hover for the original diagnostic output."

def preview(url: str) -> dict[str, object]:
    provider = provider_for(url)
    if not provider:
        raise ValueError("Please enter a valid public http(s) page link.")
    result = resolve(provider, url)
    record_resolution(ROOT, url, result)
    candidate = result.candidate
    if not candidate:
        details = "; ".join(f"{attempt.strategy}: {attempt.detail}" for attempt in result.attempts)
        raise ValueError(f"No public media could be resolved. Tried: {details}")
    RESOLVED_CANDIDATES[url] = candidate
    RESOLUTION_STRATEGIES[url] = result.attempts[-1].strategy
    if candidate.direct and candidate.kind == "gif":
        DIRECT_MEDIA[url] = candidate.source_url
    token = secrets.token_urlsafe(18)
    PREVIEWS[token] = {
        "url": candidate.source_url, "headers": candidate.headers, "page_url": url,
        "strategy": RESOLUTION_STRATEGIES[url], "kind": candidate.kind, "direct": candidate.direct,
    }
    media_url = f"/api/stream/{token}" if candidate.source_url else ""
    return {"title": candidate.title, "thumbnail": candidate.thumbnail_url, "media_url": media_url,
            "editor_token": token,
            "duration": candidate.duration_label, "seconds": candidate.duration_seconds,
            "resolution": candidate.resolution, "fallback": "reddit_gif" if candidate.kind == "gif" else ""}

def download(job: dict[str,str], provider: ProviderAdapter) -> None:
    try:
        if direct_url := job.get("direct_url"):
            ffmpeg = ffmpeg_binary()
            target_path = Path(job["path"])
            if job["mode"] == "video":
                if not ffmpeg: raise RuntimeError("FFmpeg is required to convert GIF to MP4.")
                temp_gif = target_path.with_suffix(".gif")
                request = Request(direct_url, headers={"User-Agent": "Mozilla/5.0", "Referer": job["url"]})
                try:
                    with urlopen(request, timeout=45) as source, open(temp_gif, "wb") as destination:
                        shutil.copyfileobj(source, destination)
                except OSError as exc:
                    raise RuntimeError(f"Direct GIF download failed: {exc}") from exc
                conversion = run_hidden([
                    ffmpeg, "-y", "-i", str(temp_gif), "-movflags", "faststart",
                    "-pix_fmt", "yuv420p", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", str(target_path)
                ], capture_output=True, text=True, encoding="utf-8", errors="replace")
                temp_gif.unlink(missing_ok=True)
                if conversion.returncode != 0:
                    raise RuntimeError(f"GIF to MP4 conversion failed: {conversion.stderr[-500:]}")
                job["log"] = "Converted public Reddit GIF to MP4."
                job["status"] = "complete"
                return
            request = Request(direct_url, headers={"User-Agent": "Media-Downloader-Tool/1.0 (public GIF download)", "Referer": job["url"]})
            try:
                with urlopen(request, timeout=45) as source, open(job["path"], "wb") as destination:
                    shutil.copyfileobj(source, destination)
            except OSError as exc:
                raise RuntimeError(f"Reddit direct GIF download failed: {exc}") from exc
            job["log"] = "Downloaded public Reddit GIF directly."
            job["status"] = "complete"
            return
        if direct_media_url := job.get("direct_media_url"):
            ffmpeg = ffmpeg_binary()
            if job["mode"] in {"gif", "audio"} and not ffmpeg: raise RuntimeError("Conversion runtime is missing. Run start.bat again, then retry.")
            target_path = Path(job["path"])
            download_target = target_path if job["mode"] == "video" else target_path.with_suffix(".mp4")
            headers = {}
            if raw_headers := job.get("direct_headers"):
                try:
                    headers = json.loads(raw_headers)
                except Exception:
                    headers = {}
            if not headers:
                headers = {"User-Agent": "Mozilla/5.0", "Referer": job["url"]}

            is_manifest = ".m3u8" in direct_media_url.lower() or ".mpd" in direct_media_url.lower()
            if is_manifest:
                if not ffmpeg: raise RuntimeError("Conversion runtime is required for HLS or DASH video streams.")
                header_str = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
                cmd = [ffmpeg, "-y"]
                if header_str: cmd.extend(["-headers", header_str])
                cmd.extend(["-i", direct_media_url, "-c", "copy", str(download_target)])
                res = run_hidden(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
                if res.returncode != 0:
                    raise RuntimeError(f"FFmpeg stream download failed: {res.stderr[-500:]}")
            else:
                request = Request(direct_media_url, headers=headers)
                try:
                    with urlopen(request, timeout=60) as source, open(download_target, "wb") as destination:
                        shutil.copyfileobj(source, destination)
                except OSError as exc:
                    raise RuntimeError(f"Direct media download failed: {exc}") from exc
            returncode = 0
            output = "Direct media stream downloaded successfully."
            if job["mode"] in {"gif", "audio"}:
                intermediate = str(download_target)
                conversion_args = ([ffmpeg, "-y", "-i", intermediate, "-vf", "fps=12,scale=640:-1:flags=lanczos", str(target_path)]
                                    if job["mode"] == "gif" else [ffmpeg, "-y", "-i", intermediate, "-vn", "-codec:a", "libmp3lame", "-q:a", "2", str(target_path)])
                conversion = run_hidden(
                    conversion_args,
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                )
                output += "\n" + conversion.stdout + conversion.stderr
                returncode = conversion.returncode
                if returncode == 0:
                    Path(intermediate).unlink(missing_ok=True)
                else:
                    returncode = conversion.returncode
            job["log"] = output[-4000:]; job["status"] = "complete" if returncode == 0 else "failed"
            if job["status"] == "failed":
                record_download_failure(ROOT, job["url"], provider.name, job.get("strategy", "download"), output)
                job["code"], job["error"] = classify_failure(output)
            return
        ffmpeg = ffmpeg_binary()
        if job["mode"] in {"gif", "audio"} and not ffmpeg: raise RuntimeError("Conversion runtime is missing. Run start.bat again, then retry.")
        from yt_dlp import YoutubeDL

        class DownloadLogger:
            def __init__(self) -> None: self.messages: list[str] = []
            def debug(self, message: str) -> None: self.messages.append(message)
            def warning(self, message: str) -> None: self.messages.append(f"WARNING: {message}")
            def error(self, message: str) -> None: self.messages.append(f"ERROR: {message}")

        output_template = str(Path(job["path"]).with_suffix("")) + ".%(ext)s"
        logger = DownloadLogger()
        options = yt_dlp_options()
        options.update({
            "outtmpl": output_template,
            "logger": logger,
            "merge_output_format": "mp4",
            # merge_output_format only applies to split streams. Remux a
            # single-stream WebM fallback too, so the selected .mp4 path is
            # always the file produced by a successful video download.
            "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
        })
        if ffmpeg: options["ffmpeg_location"] = ffmpeg
        with YoutubeDL(options) as downloader:
            result = downloader.download([job["url"]])
        output = "\n".join(logger.messages)
        returncode = int(result or 0)
        if returncode == 0 and job["mode"] in {"gif", "audio"}:
            intermediate = str(Path(job["path"]).with_suffix(".mp4"))
            conversion_args = ([ffmpeg, "-y", "-i", intermediate, "-vf", "fps=12,scale=640:-1:flags=lanczos", job["path"]]
                                if job["mode"] == "gif" else [ffmpeg, "-y", "-i", intermediate, "-vn", "-codec:a", "libmp3lame", "-q:a", "2", job["path"]])
            conversion = run_hidden(
                conversion_args,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            output += conversion.stdout + conversion.stderr
            if conversion.returncode == 0:
                Path(intermediate).unlink(missing_ok=True)
            else:
                returncode = conversion.returncode
        job["log"] = output[-4000:]; job["status"] = "complete" if returncode == 0 else "failed"
        if job["status"] == "failed":
            record_download_failure(ROOT, job["url"], provider.name, job.get("strategy", "download"), output)
            job["code"], job["error"] = classify_failure(output)
    except Exception as exc:
        record_download_failure(ROOT, job["url"], provider.name, job.get("strategy", "download"), str(exc))
        job["status"] = "failed"; job["log"] = str(exc); job["code"], job["error"] = classify_failure(str(exc))

PAGE = r'''<!doctype html><html><head><meta charset="utf-8"><title>Media Downloader</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%23152848'/%3E%3Cpath d='M16 6v14m-6-6 6 6 6-6M8 23v3h16v-3' fill='none' stroke='%2387cefa' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E">
<style>
body{font-family:system-ui,sans-serif;max-width:780px;margin:24px auto 36px;background:#10131a;color:#eef2ff;padding:0 20px}
h1{margin:0;font-size:24px}
.launch-row{display:flex;align-items:center;gap:16px}.launch-row h1{flex:0 0 auto;white-space:nowrap}.launch-row .entry{flex:1;min-width:0}
.mode-switch{display:inline-flex;background:#0d1117;border:1px solid #2e3950;border-radius:8px;padding:3px}
.mode-switch button{border:0;background:transparent;color:#8c9cb8;font-weight:700;padding:7px 12px;border-radius:6px;cursor:pointer}
.mode-switch button.active{background:#273650;color:#76a7ff}
.app-view[hidden]{display:none!important}
form{background:#1c2230;border:1px solid #2e3950;border-radius:10px;padding:16px 18px;margin:14px 0}
.entry{display:flex;gap:0}
.entry input{min-width:0;flex:1;box-sizing:border-box;padding:12px;border-radius:6px 0 0 6px;border:1px solid #4b5b7c;background:#111722;color:#fff;font-size:15px}
.entry input:focus,.entry input:focus-visible{outline:none !important;box-shadow:none !important;border-color:#4b5b7c !important}
.entry button{display:inline-flex;align-items:center;justify-content:center;gap:6px;min-width:52px;border:0;font-weight:700;cursor:pointer;margin:0;border-radius:0;padding:10px 18px;background:#76a7ff;color:#10131a;font-size:14px;transition:background .15s ease,color .15s ease,min-width .15s ease}
.entry .open-video-button{border-left:1px solid rgba(16,19,26,.35);border-radius:0 6px 6px 0;padding-inline:15px;background:#9cbaff}
.entry .open-video-button:hover{background:#b3c9ff}
.entry button svg{display:block;pointer-events:none}
.entry button:focus,.entry button:focus-visible{outline:none !important;box-shadow:none !important}
.entry button:disabled{background:#526174;cursor:wait;color:#cbd5e1}

/* Format Pills */
.format-options{display:flex;justify-content:center;align-items:center;flex-wrap:wrap;gap:8px;margin:12px auto 8px}
.format-label{font-size:13px;font-weight:600;color:#aab4cd}
.format-pills{display:inline-flex;background:#111722;border:1px solid #2e3950;border-radius:6px;padding:3px;gap:4px}
.format-pill{border:0;background:transparent;color:#8c9cb8;font-weight:700;font-size:13px;min-height:36px;padding:6px 14px;border-radius:4px;cursor:pointer;transition:all .15s ease}
.format-pill:hover{color:#fff}
.format-pill.active{background:#76a7ff;color:#10131a;box-shadow:0 1px 3px rgba(0,0,0,0.3)}

/* Quick Save Section */
.quick-save-section{margin-top:14px;background:#141a25;border:1px solid #2d384d;border-radius:8px;padding:12px 14px}
.quick-save-header{display:flex;align-items:center;justify-content:flex-end;margin-bottom:10px}
.toggle-group{display:inline-flex;background:#0d1117;border:1px solid #2e3950;border-radius:6px;padding:2px;cursor:pointer;user-select:none}
.toggle-btn{background:transparent;border:0;color:#8c9cb8;font-size:12px;font-weight:600;padding:5px 14px;border-radius:4px;cursor:pointer;transition:all .15s ease}
.toggle-btn:hover{color:#fff}
.toggle-btn.active{background:#273650;color:#76a7ff;box-shadow:0 1px 3px rgba(0,0,0,0.3)}

.folder-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
@media (max-width:640px){.folder-grid{grid-template-columns:repeat(2,1fr)}}
@media (max-width:440px){.folder-grid{grid-template-columns:1fr}}

.folder-chip{position:relative;z-index:1;display:flex;align-items:center;gap:10px;background:#1a2232;border:1px solid #2d3b54;border-radius:8px;padding:8px 12px;cursor:pointer;text-align:left;transition:background .15s ease,border-color .15s ease;user-select:none;color:#eef2ff;font:inherit;box-sizing:border-box;min-height:50px;overflow:hidden}
.folder-chip:hover{position:relative;z-index:5;background:#232f45;border-color:#76a7ff;transform:none !important}
.folder-chip.saving{border-color:#76a7ff;background:#202b3e;pointer-events:none}
.folder-chip.saving .folder-text{padding-right:36px}
.folder-icon{font-size:18px;line-height:1;flex-shrink:0}
.folder-text{min-width:0;flex:1;display:flex;flex-direction:column;gap:2px}
.folder-name{font-weight:600;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.folder-sub{font-size:10px;color:#8c9cb8;line-height:1.25;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;direction:rtl;text-align:left}
.folder-sub bdi{direction:ltr;unicode-bidi:isolate}

/* Custom Folder Chip */
.folder-chip.custom-folder-chip{background:#25334c;border-color:#3e537a}
.folder-chip.custom-folder-chip:hover{background:#2f405f;border-color:#9cbaff;transform:none !important;z-index:5}

/* View More Folders Button */
.view-more-container{display:flex;justify-content:center;align-items:center;margin-top:12px}
.view-more-btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;background:#1a2232;border:1px solid #2d3b54;border-radius:20px;color:#8c9cb8;font-size:12px;font-weight:600;padding:6px 20px;cursor:pointer;transition:all .18s ease;user-select:none}
.view-more-btn:hover{background:#232f45;border-color:#76a7ff;color:#eef2ff;transform:none;box-shadow:0 2px 8px rgba(0,0,0,0.25)}
.view-more-btn:active{transform:none}
.view-more-btn svg{transition:transform .22s cubic-bezier(0.16,1,0.3,1)}
.view-more-btn.expanded svg{transform:rotate(180deg)}

/* Folder List Smooth Animated Wrapper */
.folder-list-wrapper{position:relative;overflow:hidden;transition:height .34s cubic-bezier(0.16,1,0.3,1)}

/* Folder Chip In-Button HUD (centered on the right side with blur) */
.chip-hud{position:absolute;right:10px;top:50%;transform:translateY(-50%) scale(0.75);width:34px;height:34px;border-radius:50%;background:rgba(18,24,38,0.82);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border:1px solid rgba(255,255,255,0.16);box-shadow:0 4px 14px rgba(0,0,0,0.4);display:flex;align-items:center;justify-content:center;opacity:0;pointer-events:none;z-index:2;transition:opacity .22s cubic-bezier(0.16,1,0.3,1),transform .22s cubic-bezier(0.16,1,0.3,1),border-color .25s ease,box-shadow .25s ease}
.chip-hud.visible{opacity:1;transform:translateY(-50%) scale(1)}
.chip-hud.success{border-color:rgba(34,197,94,0.5);box-shadow:0 4px 16px rgba(0,0,0,0.45),0 0 16px rgba(34,197,94,0.4);animation:chipHudPop .4s cubic-bezier(0.175,0.885,0.32,1.275)}
@keyframes chipHudPop{0%{transform:translateY(-50%) scale(0.85)}50%{transform:translateY(-50%) scale(1.1)}100%{transform:translateY(-50%) scale(1)}}

/* Chip HUD Spinner */
.chip-spinner{display:block;animation:chipSpin .9s linear infinite;transform-origin:center}
.chip-spinner-arc{stroke-dasharray:55;stroke-dashoffset:15;animation:chipDash 1.2s ease-in-out infinite alternate}
@keyframes chipSpin{100%{transform:rotate(360deg)}}
@keyframes chipDash{0%{stroke-dashoffset:45}100%{stroke-dashoffset:10}}

/* Chip HUD Checkmark */
.chip-check-icon{display:block}
.chip-check-circle{animation:chipCircleGrow .3s cubic-bezier(0.16,1,0.3,1) forwards;transform-origin:center}
.chip-check-path{stroke-dasharray:24;stroke-dashoffset:24;animation:chipDrawCheck .38s .1s cubic-bezier(0.16,1,0.3,1) forwards}
@keyframes chipCircleGrow{0%{transform:scale(0.5);opacity:0}100%{transform:scale(1);opacity:1}}
@keyframes chipDrawCheck{0%{stroke-dashoffset:24}100%{stroke-dashoffset:0}}

/* Media Preview Expansion/Collapse Animation without blurry GPU layer */
.preview-container {
  display: grid;
  grid-template-rows: 0fr;
  opacity: 0;
  margin: 0;
  padding: 0;
  transform: translateY(0) scale(1);
  transition: grid-template-rows 0.48s cubic-bezier(0.16, 1, 0.3, 1),
              opacity 0.32s ease,
              transform 0.48s cubic-bezier(0.16, 1, 0.3, 1);
}
.preview-container.expanded {
  grid-template-rows: 1fr;
  opacity: 1;
}
.preview-container.saving-away{opacity:0;transform:translateY(-8px) scale(.99);pointer-events:none}
.preview-content {
  min-height: 0;
  overflow: hidden;
}
@media (prefers-reduced-motion: reduce) {
  .preview-container, .folder-list-wrapper {
    transition: none !important;
  }
}
/* Preview Section Card (Matching Quick Save Card) */
.preview-card{margin-top:14px;background:#141a25;border:1px solid #2d384d;border-radius:8px;padding:12px 14px;display:flex;flex-direction:column;gap:10px;-webkit-font-smoothing:subpixel-antialiased;overflow:hidden;transition:background .52s cubic-bezier(.16,1,.3,1),border-radius .52s cubic-bezier(.16,1,.3,1),padding .52s cubic-bezier(.16,1,.3,1),gap .52s cubic-bezier(.16,1,.3,1)}
.preview-card.editor-expanded{background:#101620;border-radius:10px;padding:0;gap:0}

/* Minimized Video Preview Button */
.thumb-preview-wrap{display:flex;width:100%;max-height:68px;opacity:1;transform:translateY(0) scale(1);overflow:hidden;transition:max-height .52s cubic-bezier(.16,1,.3,1),opacity .28s ease,transform .52s cubic-bezier(.16,1,.3,1)}
.preview-card.editor-expanded .thumb-preview-wrap{max-height:0;opacity:0;transform:translateY(12px) scale(.985);pointer-events:none}
.thumb-preview-btn{display:flex;align-items:center;gap:12px;background:#1a2232;border:1px solid #2d3b54;border-radius:8px;padding:8px 12px;width:100%;cursor:pointer;text-align:left;color:#eef2ff;font:inherit;transition:background .15s ease,border-color .15s ease;box-sizing:border-box;user-select:none;min-height:50px}
.thumb-preview-btn:hover{background:#232f45;border-color:#76a7ff}
.thumb-media-box{position:relative;width:68px;height:42px;border-radius:6px;overflow:hidden;background:#0d1117;flex-shrink:0;display:flex;align-items:center;justify-content:center}
.thumb-img{width:100%;height:100%;object-fit:cover;display:block}
.local-thumb-video{pointer-events:none}
.thumb-placeholder-icon{font-size:20px;line-height:1}
.thumb-play-badge{position:absolute;inset:0;background:rgba(0,0,0,0.35);display:flex;align-items:center;justify-content:center;color:#fff;pointer-events:none}
.thumb-play-badge svg{transition:transform .18s ease}
.thumb-preview-btn:hover .thumb-play-badge svg{transform:scale(1.12)}
.thumb-meta{display:inline-flex;align-items:center;gap:8px;min-width:0;flex:1;overflow:hidden;justify-content:flex-start}
.thumb-title{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#eef2ff;min-width:0;flex-shrink:1}
.thumb-duration{background:#24324a;padding:2px 7px;border-radius:4px;color:#cbd5e1;font-weight:600;font-size:11px;line-height:1.2;white-space:nowrap;flex-shrink:0}
.thumb-preview-btn .format-options{position:relative;z-index:2;flex-wrap:nowrap;flex-shrink:0;padding:10px;margin:-10px -6px -10px -10px}
.thumb-expand-icon{color:#8c9cb8;flex-shrink:0;margin-left:0;display:block;transition:color .15s ease}
.thumb-preview-btn:hover .thumb-expand-icon{color:#76a7ff}

/* Fullscreen Popup Modal with Blurred Backdrop */
.preview-modal{position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;padding:24px;box-sizing:border-box;opacity:0;pointer-events:none;transition:opacity .24s cubic-bezier(0.16,1,0.3,1);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);background:rgba(8,11,18,0.78);cursor:pointer}
.preview-modal.open{opacity:1;pointer-events:auto}
.preview-modal-shell{position:relative;display:flex;flex-direction:column;align-items:center;justify-content:center;max-width:92vw;max-height:88vh;transform:scale(0.92);transition:transform .28s cubic-bezier(0.16,1,0.3,1);cursor:default}
.preview-modal.open .preview-modal-shell{transform:scale(1)}
.preview-modal-content{display:flex;align-items:center;justify-content:center;background:#090d14;border:1px solid rgba(255,255,255,0.14);border-radius:12px;overflow:hidden;box-shadow:0 24px 60px rgba(0,0,0,0.85);max-width:90vw;max-height:84vh}
.preview-modal-content video,.preview-modal-content img{display:block;max-width:88vw;max-height:82vh;width:auto;height:auto;object-fit:contain;background:#000}
.spinner{display:inline-block;width:13px;height:13px;border:2px solid #cbd5e1;border-right-color:transparent;border-radius:50%;animation:spin .7s linear infinite;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}

/* Error Dialog */
dialog{position:relative;max-width:680px;width:calc(100% - 40px);background:#1c2230;color:#eef2ff;border:1px solid #4b5b7c;border-radius:10px;padding:20px}
dialog::backdrop{background:#0009}
.dialog-controls{position:absolute;top:10px;right:10px;display:flex;align-items:center;gap:8px}
.icon-button{display:grid;place-items:center;width:36px;height:36px;padding:0;border:0;border-radius:6px;background:#273650;color:#eef2ff;cursor:pointer}
.close-button{background:transparent;font-size:24px;line-height:1}
#errorText{margin:8px 160px 0 0;white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;color:inherit}
.copy-status{color:#aab4cd;font-size:13px}
.editor-card{background:#1c2230;border:1px solid #2e3950;border-radius:10px;padding:12px;margin:10px 0;overflow:hidden;opacity:1;transform:translateY(0) scale(1);transition:max-height .48s cubic-bezier(.16,1,.3,1),opacity .32s ease,transform .48s cubic-bezier(.16,1,.3,1),margin .48s cubic-bezier(.16,1,.3,1),padding .48s cubic-bezier(.16,1,.3,1),border-width .48s ease}
.editor-card.editor-opening{opacity:0;transform:translateY(-10px) scale(.985)}
.editor-card.editor-minimizing{opacity:0;transform:translateY(-10px) scale(.985);margin-top:0;margin-bottom:0;padding-top:0;padding-bottom:0;border-width:0;pointer-events:none}
.preview-card .editor-card{margin:2px 0 0;background:#101620;border-color:#2d384d}
.preview-card.editor-expanded .editor-card{margin:0;background:transparent;border:0}
.preview-card:not(.editor-expanded) .thumb-media-box{view-transition-name:editor-media}
.preview-card.editor-expanded .editor-preview-frame{view-transition-name:editor-media}
.preview-card:not(.editor-expanded) .thumb-title{view-transition-name:editor-title}
.preview-card.editor-expanded .editor-toolbar-title{view-transition-name:editor-title}
::view-transition-group(editor-media),::view-transition-group(editor-title){animation-duration:.52s;animation-timing-function:cubic-bezier(.16,1,.3,1)}
::view-transition-old(editor-media),::view-transition-new(editor-media){height:100%;object-fit:cover;overflow:hidden}
.upload-drop{display:flex;align-items:center;justify-content:center;width:100%;min-height:126px;border:1px dashed #4b5b7c;border-radius:9px;background:#141a25;color:#cbd5e1;cursor:pointer;text-align:center;padding:20px;box-sizing:border-box;font:inherit;transition:border-color .15s ease,background .15s ease}
.upload-drop:hover,.upload-drop.dragover{border-color:#76a7ff;background:#182235}
.upload-drop strong{display:block;color:#fff;margin-bottom:5px}
.editor-workspace{margin-top:0}
.editor-preview-frame{position:relative;width:100%;max-height:430px;overflow:hidden;border-radius:9px;background:#070a10;border:1px solid #2d384d;isolation:isolate}
.editor-preview-frame video{position:absolute;display:block;max-width:none;max-height:none;object-fit:fill;background:#000;cursor:pointer}
.crop-overlay{position:absolute;inset:0;z-index:3;touch-action:none;user-select:none;cursor:crosshair}
.crop-box{position:absolute;box-sizing:border-box;border:2px solid #76a7ff;box-shadow:0 0 0 9999px rgba(3,7,13,.72),0 0 0 1px rgba(255,255,255,.34) inset;cursor:move}
.crop-box::before,.crop-box::after{content:"";position:absolute;pointer-events:none;background:rgba(255,255,255,.48)}
.crop-box::before{left:33.333%;top:0;width:1px;height:100%;box-shadow:calc(var(--crop-box-width, 0px)/3) 0 0 rgba(255,255,255,.48)}
.crop-box::after{left:0;top:33.333%;width:100%;height:1px;box-shadow:0 calc(var(--crop-box-height, 0px)/3) 0 rgba(255,255,255,.48)}
.crop-handle{position:absolute;background:#fff;border:2px solid #76a7ff;box-shadow:0 2px 9px #000b;box-sizing:border-box}
.crop-handle.corner{width:16px;height:16px;border-radius:50%}
.crop-handle[data-handle="nw"]{left:-9px;top:-9px;cursor:nwse-resize}.crop-handle[data-handle="ne"]{right:-9px;top:-9px;cursor:nesw-resize}.crop-handle[data-handle="sw"]{left:-9px;bottom:-9px;cursor:nesw-resize}.crop-handle[data-handle="se"]{right:-9px;bottom:-9px;cursor:nwse-resize}
.crop-handle[data-handle="w"],.crop-handle[data-handle="e"]{top:50%;width:12px;height:46px;border-radius:6px;transform:translateY(-50%);cursor:ew-resize}.crop-handle[data-handle="w"]{left:-7px}.crop-handle[data-handle="e"]{right:-7px}
.crop-handle[data-handle="n"],.crop-handle[data-handle="s"]{left:50%;width:46px;height:12px;border-radius:6px;transform:translateX(-50%);cursor:ns-resize}.crop-handle[data-handle="n"]{top:-7px}.crop-handle[data-handle="s"]{bottom:-7px}
.crop-badge{position:absolute;left:8px;top:8px;padding:4px 7px;border-radius:5px;background:rgba(8,13,22,.78);color:#fff;font-size:11px;font-weight:700;pointer-events:none;backdrop-filter:blur(6px)}
.editor-toolbar{display:flex;align-items:center;gap:7px;margin-bottom:8px;padding:6px;background:#141a25;border:1px solid #2d384d;border-radius:8px}
.editor-toolbar-title{min-width:0;flex:1;text-align:center;color:#eef2ff;font-size:13px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.aspect-select{min-width:104px;border:1px solid #4b5b7c;border-radius:6px;background:#111722;color:#eef2ff;padding:8px 30px 8px 9px;font-weight:700;cursor:pointer}
.toolbar-spacer{flex:1}.toolbar-button{display:inline-flex;align-items:center;justify-content:center;gap:7px;min-height:38px;border:1px solid #3e537a;background:#25334c;color:#eef2ff;border-radius:6px;padding:8px 12px;font-weight:750;cursor:pointer}.toolbar-button:hover{border-color:#76a7ff}.toolbar-button.active{background:#76a7ff;border-color:#76a7ff;color:#10131a}.toolbar-button.secondary{background:#111722;color:#aab4cd}
.media-controls{display:flex;align-items:center;justify-content:center;gap:9px;margin-top:7px}
.media-control{display:grid;place-items:center;width:36px;height:36px;padding:0;border:0;background:transparent;color:#dbe5f7;cursor:pointer;transition:color .15s ease,opacity .15s ease,transform .15s ease}
.media-control:hover{color:#fff;transform:scale(1.08)}
.media-control:active{transform:scale(.92)}
.media-control.marker-control{color:#ffd84d}.media-control.marker-control:hover{color:#ffe783}
.media-control svg{pointer-events:none}
.editor-controls{display:grid;grid-template-columns:1fr;gap:10px;margin-top:10px}
.control-card{background:#141a25;border:1px solid #2d384d;border-radius:8px;padding:9px 11px}
.control-title{display:block;font-size:12px;font-weight:700;color:#aab4cd;margin-bottom:8px}
.clip-timeline{position:relative;height:34px;margin:10px 9px 4px;background:#0b1019;border:1px solid #2e3950;border-radius:6px;cursor:pointer}
.clip-selection{position:absolute;top:8px;height:16px;border-radius:3px;background:rgba(255,216,77,.34);border-top:1px solid rgba(255,224,112,.8);border-bottom:1px solid rgba(153,119,0,.8);pointer-events:none}
.clip-handle{position:absolute;z-index:2;top:3px;width:12px;height:26px;padding:0;border:0;border-radius:3px;background:#ffd84d;box-shadow:0 2px 8px #0009;cursor:ew-resize;transform:translateX(-50%)}
.clip-handle::after{content:"";position:absolute;left:5px;top:7px;width:2px;height:12px;border-radius:1px;background:#796100}
.clip-playhead{position:absolute;z-index:1;top:2px;bottom:2px;width:2px;background:#eef2ff;box-shadow:0 0 5px #76a7ff;transform:translateX(-50%);pointer-events:none}
.clip-time-summary{display:flex;justify-content:space-between;color:#8c9cb8;font-size:11px;font-variant-numeric:tabular-nums}
.editor-status{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
.preserve-toggle{display:flex;align-items:center;gap:6px;color:#cbd5e1;font-size:11px;font-weight:650;cursor:pointer;user-select:none;white-space:nowrap;flex-shrink:0}.preserve-toggle input{position:absolute;opacity:0;pointer-events:none}.toggle-track{position:relative;width:30px;height:18px;border-radius:999px;background:#29354a;border:1px solid #455675;transition:background .15s ease,border-color .15s ease;flex-shrink:0}.toggle-track::after{content:"";position:absolute;left:3px;top:3px;width:10px;height:10px;border-radius:50%;background:#cbd5e1;transition:transform .18s ease,background .18s ease}.preserve-toggle input:checked+.toggle-track{background:#76a7ff;border-color:#76a7ff}.preserve-toggle input:checked+.toggle-track::after{transform:translateX(12px);background:#10131a}.preserve-toggle input:focus-visible+.toggle-track{outline:2px solid #fff;outline-offset:2px}
@media (max-width:640px){.launch-row{align-items:stretch;flex-direction:column}.editor-toolbar{flex-wrap:nowrap;gap:5px}.aspect-select{flex:0 1 92px;min-width:82px;padding-right:22px}.preserve-toggle{gap:4px}.editor-toolbar-title{order:initial;flex:1 1 auto;min-width:0;text-align:center}.toolbar-button{padding-inline:8px}.editor-card{padding:9px}}
@media (prefers-reduced-motion:reduce){.preview-card,.thumb-preview-wrap,.editor-card{transition:none!important}::view-transition-group(editor-media),::view-transition-group(editor-title){animation-duration:0s}}
</style></head><body>
<main id="downloaderView" class="app-view">
<form id="form" autocomplete="off" onsubmit="event.preventDefault()">
<div class="launch-row"><h1>Media Downloader</h1><div class="entry"><input id="url" type="text" placeholder="Paste a public page or media link" required autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" data-lpignore="true"><button id="action" type="button" aria-label="Paste link from clipboard and load preview" title="Paste link from clipboard and load preview"></button><button id="openVideoButton" class="open-video-button" type="button" aria-label="Open a video to edit" title="Open a video to edit"><svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="m10 12 5 3-5 3z"/></svg></button></div></div>

<div id="preview" class="preview-container"><div id="previewContent" class="preview-content"></div></div>

<div id="editorHome">
<section id="editorView" class="editor-card" hidden>
    <div id="editorWorkspace" class="editor-workspace" hidden>
      <div class="editor-toolbar">
        <select id="aspectSelect" class="aspect-select" aria-label="Crop aspect ratio">
          <option value="free">Aspect ratio</option>
          <option value="original">Original</option>
          <option value="1:1">1:1</option>
          <option value="4:5">4:5</option>
          <option value="9:16">9:16</option>
          <option value="16:9">16:9</option>
        </select>
        <label class="preserve-toggle" title="Preserve original source video" aria-label="Preserve original source video"><input id="preserveOriginal" type="checkbox"><span class="toggle-track" aria-hidden="true"></span><span>Preserve source</span></label>
        <strong id="editorSourceName" class="editor-toolbar-title">Video preview</strong>
        <button id="toggleCrop" class="toolbar-button" type="button" aria-label="Adjust crop" title="Adjust crop">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 2v14a2 2 0 0 0 2 2h14"/><path d="M18 22V8a2 2 0 0 0-2-2H2"/></svg><span>Crop</span>
        </button>
        <button id="resetCrop" class="toolbar-button secondary" type="button">Reset</button>
      </div>
      <div id="editorPreviewFrame" class="editor-preview-frame">
        <video id="editorVideo" playsinline></video>
        <div id="cropOverlay" class="crop-overlay" hidden>
          <div id="cropBox" class="crop-box">
            <span id="cropBadge" class="crop-badge">Free crop</span>
            <i class="crop-handle corner" data-handle="nw"></i><i class="crop-handle corner" data-handle="ne"></i>
            <i class="crop-handle corner" data-handle="sw"></i><i class="crop-handle corner" data-handle="se"></i>
            <i class="crop-handle" data-handle="w"></i><i class="crop-handle" data-handle="e"></i>
            <i class="crop-handle" data-handle="n"></i><i class="crop-handle" data-handle="s"></i>
          </div>
        </div>
      </div>
      <div class="editor-controls">
        <section class="control-card full">
          <div id="clipTimeline" class="clip-timeline" aria-label="Clip range">
            <div id="clipSelection" class="clip-selection"></div>
            <button id="clipStartHandle" class="clip-handle" type="button" aria-label="Clip start"></button>
            <button id="clipEndHandle" class="clip-handle" type="button" aria-label="Clip end"></button>
            <div id="clipPlayhead" class="clip-playhead"></div>
          </div>
          <div class="clip-time-summary"><span id="clipStartLabel">0.000s</span><span id="clipDurationLabel">Full video</span><span id="clipEndLabel">0.000s</span></div>
          <div class="media-controls" aria-label="Playback controls">
            <button id="previousFrame" class="media-control" type="button" aria-label="Previous frame" title="Previous frame"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m15 18-6-6 6-6"/></svg></button>
            <button id="startClip" class="media-control marker-control" type="button" aria-label="Set clip start at current position" title="Set clip start"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 4v16"/><path d="M11 7h6v10h-6"/></svg></button>
            <button id="playPause" class="media-control" type="button" aria-label="Play" title="Play"><svg id="playPauseIcon" viewBox="0 0 24 24" width="22" height="22" fill="currentColor"><path d="M8 5v14l11-7z"/></svg></button>
            <button id="endClip" class="media-control marker-control" type="button" aria-label="Set clip end at current position" title="Set clip end"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 4v16"/><path d="M13 7H7v10h6"/></svg></button>
            <button id="nextFrame" class="media-control" type="button" aria-label="Next frame" title="Next frame"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg></button>
            <button id="replayClip" class="media-control marker-control" type="button" aria-label="Replay selected clip" title="Replay selected clip"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v6h6"/></svg></button>
          </div>
          <input id="trimStartNumber" type="number" value="0" hidden>
          <input id="trimEndNumber" type="number" value="0" hidden>
          <input id="trimStartRange" type="range" min="0" max="0" step="0.001" value="0" hidden>
          <input id="trimEndRange" type="range" min="0" max="0" step="0.001" value="0" hidden>
        </section>
      </div>
      <div id="editorStatus" class="editor-status" role="status" aria-live="polite"></div>
    </div>
</section>
</div>

<section class="quick-save-section" id="quickSaveSection">
  <div class="quick-save-header">
    <div class="toggle-group" role="group" aria-label="Toggle folder sorting" title="Click to toggle between Most Frequent and Recent">
      <button type="button" id="toggleFrequent" class="toggle-btn active" title="Click to toggle between Most Frequent and Recent">Most Frequent</button>
      <button type="button" id="toggleRecent" class="toggle-btn" title="Click to toggle between Most Frequent and Recent">Recent</button>
    </div>
  </div>
  <div id="folderListWrapper" class="folder-list-wrapper">
    <div id="folderList" class="folder-grid"></div>
  </div>
  <div id="viewMoreContainer" class="view-more-container" style="display:none">
    <button type="button" id="viewMoreBtn" class="view-more-btn" aria-expanded="false" title="Show more folders">
      <span id="viewMoreText">View More</span>
      <svg id="viewMoreIcon" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <polyline points="6 9 12 15 18 9"></polyline>
      </svg>
    </button>
  </div>
</section>

<dialog id="errorDialog" aria-labelledby="errorText">
  <div class="dialog-controls">
    <span class="copy-status" id="copyStatus" role="status" hidden>Copied</span>
    <button class="icon-button" id="copyError" type="button" aria-label="Copy error details" title="Copy error details">
      <svg aria-hidden="true" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="11" height="11" rx="1"></rect><path d="M15 9V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v9a1 1 0 0 0 1 1h4"></path></svg>
    </button>
    <button class="icon-button close-button" id="closeError" type="button" aria-label="Close error dialog">×</button>
  </div>
  <pre id="errorText"></pre>
</dialog>
</form>
</main>

<div id="previewModal" class="preview-modal" aria-hidden="true" style="display:none">
  <div class="preview-modal-shell" id="previewModalShell">
    <div id="previewModalContent" class="preview-modal-content"></div>
  </div>
</div>

<script>
const input = document.getElementById('url'),
      button = document.getElementById('action'),
      output = document.getElementById('preview'),
      previewContent = document.getElementById('previewContent'),
      previewModal = document.getElementById('previewModal'),
      previewModalContent = document.getElementById('previewModalContent'),
      dialog = document.getElementById('errorDialog'),
      errorText = document.getElementById('errorText'),
      copyStatus = document.getElementById('copyStatus'),
      folderList = document.getElementById('folderList'),
      folderListWrapper = document.getElementById('folderListWrapper'),
      toggleGroup = document.querySelector('.toggle-group'),
      toggleFrequent = document.getElementById('toggleFrequent'),
      toggleRecent = document.getElementById('toggleRecent'),
      viewMoreContainer = document.getElementById('viewMoreContainer'),
      viewMoreBtn = document.getElementById('viewMoreBtn'),
      viewMoreText = document.getElementById('viewMoreText');

const openVideoButton = document.getElementById('openVideoButton'),
      editorHome = document.getElementById('editorHome'),
      editorView = document.getElementById('editorView'),
      editorSourceName = document.getElementById('editorSourceName'),
      editorWorkspace = document.getElementById('editorWorkspace'),
      editorPreviewFrame = document.getElementById('editorPreviewFrame'),
      editorVideo = document.getElementById('editorVideo'),
      previousFrame = document.getElementById('previousFrame'),
      nextFrame = document.getElementById('nextFrame'),
      startClip = document.getElementById('startClip'),
      endClip = document.getElementById('endClip'),
      replayClip = document.getElementById('replayClip'),
      playPause = document.getElementById('playPause'),
      playPauseIcon = document.getElementById('playPauseIcon'),
      cropOverlay = document.getElementById('cropOverlay'),
      cropBox = document.getElementById('cropBox'),
      cropBadge = document.getElementById('cropBadge'),
      toggleCrop = document.getElementById('toggleCrop'),
      resetCrop = document.getElementById('resetCrop'),
      trimStartNumber = document.getElementById('trimStartNumber'),
      trimEndNumber = document.getElementById('trimEndNumber'),
      trimStartRange = document.getElementById('trimStartRange'),
      trimEndRange = document.getElementById('trimEndRange'),
      clipTimeline = document.getElementById('clipTimeline'),
      clipSelection = document.getElementById('clipSelection'),
      clipStartHandle = document.getElementById('clipStartHandle'),
      clipEndHandle = document.getElementById('clipEndHandle'),
      clipPlayhead = document.getElementById('clipPlayhead'),
      clipStartLabel = document.getElementById('clipStartLabel'),
      clipEndLabel = document.getElementById('clipEndLabel'),
      clipDurationLabel = document.getElementById('clipDurationLabel'),
      aspectSelect = document.getElementById('aspectSelect'),
      editorStatus = document.getElementById('editorStatus'),
      preserveOriginal = document.getElementById('preserveOriginal');

let selectedUrl = '',
    previewData = null,
    currentMode = 'video',
    folderSortMode = localStorage.getItem('mdt_folder_sort') || 'frequent',
    foldersExpanded = false,
    foldersData = { frequent: [], recent: [] },
    activeJobUrl = '',
    pollTimer = null,
    activeChipElement = null,
    chipHudTimer = null,
    collapseAnimTimer = null,
    folderAnimTimer = null,
    copyStatusTimer = null,
    editorUploadId = '',
    editorAspect = 'free',
    editorDuration = 0,
    editorFrameRate = 30,
    editorPollTimer = null,
    editorEditing = false,
    previewEditorToken = '',
    previewEditorPromise = null,
    localEditorData = null,
    editorAnimationTimer = null,
    clipReplayTimer = null,
    clipReplayActive = false,
    cropMode = false,
    editorCrop = {x:0, y:0, width:1, height:1},
    cropPointer = null,
    clipPointer = null;

const esc = value => String(value).replace(/[&<>"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[char]));

const PASTE_ICON_SVG = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"></path><rect x="8" y="2" width="8" height="4" rx="1" ry="1"></rect></svg>';

function update() {
  button.disabled = false;
  if (input.value.trim()) {
    button.textContent = 'Confirm';
    button.title = 'Confirm link';
    button.setAttribute('aria-label', 'Confirm link');
  } else {
    button.innerHTML = PASTE_ICON_SVG;
    button.title = 'Paste link from clipboard and load preview';
    button.setAttribute('aria-label', 'Paste link from clipboard and load preview');
  }
}

function setBusy() {
  button.disabled = true;
  button.innerHTML = '<span class="spinner"></span>';
}

function showError(code, text, raw = '') {
  errorText.textContent = code + ' — ' + text + (raw ? '\n\nDiagnostic: ' + raw : '');
  dialog.showModal();
}

function setChipLoading(chipElement) {
  clearTimeout(chipHudTimer);
  if (activeChipElement && activeChipElement !== chipElement) {
    activeChipElement.classList.remove('saving');
    const prevHud = activeChipElement.querySelector('.chip-hud');
    if (prevHud) {
      prevHud.className = 'chip-hud';
      prevHud.innerHTML = '';
    }
  }
  activeChipElement = chipElement;
  if (!chipElement) return;
  chipElement.classList.add('saving');
  const hud = chipElement.querySelector('.chip-hud');
  if (!hud) return;
  hud.className = 'chip-hud visible';
  hud.innerHTML = `
    <svg class="chip-spinner" viewBox="0 0 32 32" width="20" height="20">
      <circle cx="16" cy="16" r="12" fill="none" stroke="rgba(255, 255, 255, 0.14)" stroke-width="2.5"/>
      <circle class="chip-spinner-arc" cx="16" cy="16" r="12" fill="none" stroke="#76a7ff" stroke-width="2.5" stroke-linecap="round"/>
    </svg>`;
}

function collapsePreview(immediate = false) {
  clearTimeout(collapseAnimTimer);
  if (output) output.classList.remove('saving-away');
  closePreviewModal();
  selectedUrl = '';
  previewData = null;
  localEditorData = null;
  currentMode = 'video';
  activeJobUrl = '';

  if (foldersExpanded) {
    setFoldersExpanded(false, immediate);
  }

  const activeVideo = previewContent ? previewContent.querySelector('video') : null;
  if (activeVideo) {
    try { activeVideo.pause(); } catch (_) {}
  }
  if (editorView && editorView.closest('.preview-card')) {
    finishEditorMinimize();
  }

  if (immediate || !output || !output.classList.contains('expanded')) {
    if (output) output.classList.remove('expanded');
    if (previewContent) previewContent.innerHTML = '';
    update();
    return;
  }

  output.classList.remove('expanded');
  update();
  collapseAnimTimer = setTimeout(() => {
    if (output && !output.classList.contains('expanded')) {
      if (previewContent) previewContent.innerHTML = '';
    }
  }, 440);
}

function setChipSuccess(chipElement) {
  clearTimeout(chipHudTimer);
  const target = chipElement || activeChipElement;
  const hud = target ? target.querySelector('.chip-hud') : null;
  if (target) target.classList.add('saving');
  if (hud) {
    hud.className = 'chip-hud visible success';
    hud.innerHTML = `
      <svg class="chip-check-icon" viewBox="0 0 32 32" width="22" height="22">
        <circle class="chip-check-circle" cx="16" cy="16" r="13" fill="rgba(34, 197, 94, 0.18)" stroke="#22c55e" stroke-width="2"/>
        <path class="chip-check-path" d="M10 16.5 L14.5 21 L22 12" fill="none" stroke="#22c55e" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>`;
  }
  chipHudTimer = setTimeout(async () => {
    if (hud) {
      hud.classList.remove('visible', 'success');
      hud.innerHTML = '';
    }
    if (target) target.classList.remove('saving');
    activeChipElement = null;
    collapsePreview();
    await loadFolders();
  }, 1900);
}

function clearChipHud(chipElement) {
  clearTimeout(chipHudTimer);
  const target = chipElement || activeChipElement;
  if (target) {
    target.classList.remove('saving');
    const hud = target.querySelector('.chip-hud');
    if (hud) {
      hud.classList.remove('visible', 'success');
      hud.innerHTML = '';
    }
  }
  activeChipElement = null;
}

async function copyError() {
  let copied = false;
  try {
    await navigator.clipboard.writeText(errorText.textContent);
    copied = true;
  } catch {
    const sel = getSelection(), rng = document.createRange();
    rng.selectNodeContents(errorText);
    sel.removeAllRanges();
    sel.addRange(rng);
    copied = document.execCommand('copy');
    sel.removeAllRanges();
  }
  if (copied) {
    copyStatus.hidden = false;
    clearTimeout(copyStatusTimer);
    copyStatusTimer = setTimeout(() => copyStatus.hidden = true, 900);
  }
}

function getGridColumns() {
  if (window.matchMedia('(max-width: 440px)').matches) return 1;
  if (window.matchMedia('(max-width: 640px)').matches) return 2;
  return 3;
}

function getFittingFolderCount(totalAvailable, isExpanded) {
  const cols = getGridColumns();
  if (totalAvailable <= 0) return 0;
  if (cols === 1) {
    return isExpanded ? totalAvailable : Math.min(totalAvailable, 8);
  }

  // To guarantee Custom is at the bottom right, the total items (folders + 1 Custom)
  // must completely fill the last row: (count + 1) % cols === 0.
  // In 3 columns: count in {2, 5, 8, 11, 14, 17, 20...} (collapsed max 8)
  // In 2 columns: count in {1, 3, 5, 7, 9, 11, 13...} (collapsed max 7)
  const maxLimit = isExpanded ? totalAvailable : (cols === 3 ? 8 : 7);
  let count = Math.min(totalAvailable, maxLimit);
  while (count > 0 && (count + 1) % cols !== 0) {
    count--;
  }
  return count > 0 ? count : Math.min(totalAvailable, maxLimit);
}

function getVisibleFolders() {
  const all = foldersData[folderSortMode] || [];
  const count = getFittingFolderCount(all.length, foldersExpanded);
  return all.slice(0, count);
}

function setFoldersExpanded(expanded, immediate = false) {
  if (foldersExpanded === expanded && !immediate) return;
  clearTimeout(folderAnimTimer);
  foldersExpanded = expanded;
  viewMoreText.textContent = foldersExpanded ? 'View Less' : 'View More';
  viewMoreBtn.classList.toggle('expanded', foldersExpanded);
  viewMoreBtn.setAttribute('aria-expanded', foldersExpanded ? 'true' : 'false');
  const allFolders = foldersData[folderSortMode] || [];
  const maxExpanded = getFittingFolderCount(allFolders.length, true);
  viewMoreBtn.title = foldersExpanded ? 'Collapse folder list' : `Show all ${maxExpanded} folders`;

  if (!folderListWrapper) {
    renderFolders();
    return;
  }

  if (immediate) {
    folderListWrapper.style.transition = 'none';
    renderFolders();
    folderListWrapper.style.height = '';
    folderListWrapper.offsetHeight;
    folderListWrapper.style.transition = '';
    return;
  }

  if (foldersExpanded) {
    const startHeight = folderListWrapper.offsetHeight;
    folderListWrapper.style.height = startHeight + 'px';
    renderFolders();
    const targetHeight = folderList.scrollHeight;
    folderListWrapper.offsetHeight;
    folderListWrapper.style.height = targetHeight + 'px';
    folderAnimTimer = setTimeout(() => {
      folderListWrapper.style.height = '';
      folderAnimTimer = null;
    }, 350);
  } else {
    const startHeight = folderListWrapper.offsetHeight;
    folderListWrapper.style.height = startHeight + 'px';
    folderListWrapper.offsetHeight;

    const wrapperRect = folderListWrapper.getBoundingClientRect();
    const collapsedCount = getFittingFolderCount(allFolders.length, false);
    const targetChip = folderList.children[collapsedCount];

    let targetHeight = startHeight;
    if (targetChip) {
      const chipRect = targetChip.getBoundingClientRect();
      targetHeight = Math.round(chipRect.bottom - wrapperRect.top);
    }
    if (targetHeight >= startHeight || targetHeight <= 0) {
      const cols = getGridColumns();
      const rows = Math.ceil((collapsedCount + 1) / cols);
      targetHeight = Math.min(startHeight, rows * 58 - 8);
    }

    // Preplace the custom button with a smooth fade-in so it is already
    // present during the minimization animation without popping in abruptly
    if (targetChip) {
      const preplacedCustom = createCustomChip();
      preplacedCustom.style.opacity = '0';
      preplacedCustom.style.transition = 'opacity 0.28s cubic-bezier(0.16, 1, 0.3, 1)';
      folderList.replaceChild(preplacedCustom, targetChip);
      requestAnimationFrame(() => {
        preplacedCustom.style.opacity = '1';
      });
    }

    folderListWrapper.style.height = targetHeight + 'px';
    folderAnimTimer = setTimeout(() => {
      renderFolders();
      folderListWrapper.style.height = '';
      folderAnimTimer = null;
    }, 350);
  }
}

function createCustomChip() {
  const customChip = document.createElement('button');
  customChip.type = 'button';
  customChip.className = 'folder-chip custom-folder-chip';
  customChip.title = 'Choose a custom folder and file name using Windows file dialog';
  customChip.innerHTML = `
    <span class="folder-icon">📁</span>
    <div class="folder-text">
      <span class="folder-name">Custom</span>
    </div>
    <div class="chip-hud"></div>
  `;
  customChip.onclick = () => {
    saveToCustomFolder(customChip);
  };
  return customChip;
}

function renderFolders() {
  const allFolders = foldersData[folderSortMode] || [];
  const visibleFolders = getVisibleFolders();

  folderList.innerHTML = '';
  toggleFrequent.classList.toggle('active', folderSortMode === 'frequent');
  toggleRecent.classList.toggle('active', folderSortMode === 'recent');

  visibleFolders.forEach(folder => {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'folder-chip';
    chip.title = folder.path;

    chip.innerHTML = `
      <span class="folder-icon">📁</span>
      <div class="folder-text">
        <span class="folder-name">${esc(folder.name)}</span>
        <span class="folder-sub"><bdi>${esc(folder.path)}</bdi></span>
      </div>
      <div class="chip-hud"></div>
    `;

    chip.onclick = () => {
      handleFolderClick(folder.path, folder.name, chip);
    };

    folderList.appendChild(chip);
  });

  // Custom folder button: ALWAYS at the bottom right
  folderList.appendChild(createCustomChip());

  const maxExpanded = getFittingFolderCount(allFolders.length, true);
  const maxCollapsed = getFittingFolderCount(allFolders.length, false);
  if (maxExpanded > maxCollapsed) {
    viewMoreContainer.style.display = 'flex';
    viewMoreText.textContent = foldersExpanded ? 'View Less' : 'View More';
    viewMoreBtn.classList.toggle('expanded', foldersExpanded);
    viewMoreBtn.setAttribute('aria-expanded', foldersExpanded ? 'true' : 'false');
    viewMoreBtn.title = foldersExpanded ? 'Collapse folder list' : `Show all ${maxExpanded} folders`;
  } else {
    viewMoreContainer.style.display = 'none';
    if (foldersExpanded) {
      foldersExpanded = false;
    }
  }
}

viewMoreBtn.onclick = () => {
  setFoldersExpanded(!foldersExpanded);
};

function toggleFolderSort() {
  folderSortMode = folderSortMode === 'frequent' ? 'recent' : 'frequent';
  localStorage.setItem('mdt_folder_sort', folderSortMode);
  renderFolders();
}
if (toggleGroup) {
  toggleGroup.onclick = (e) => {
    e.preventDefault();
    toggleFolderSort();
  };
}
toggleFrequent.onclick = (e) => {
  e.preventDefault();
  e.stopPropagation();
  toggleFolderSort();
};
toggleRecent.onclick = (e) => {
  e.preventDefault();
  e.stopPropagation();
  toggleFolderSort();
};

async function loadFolders() {
  try {
    const res = await fetch('/api/folders');
    if (res.ok) {
      foldersData = await res.json();
      if (!folderAnimTimer) {
        renderFolders();
      }
    }
  } catch (_) {}
}

function openPreviewModal() {
  if (!previewData) return;
  const redditGif = previewData.fallback === 'reddit_gif';
  const media = redditGif
    ? (previewData.thumbnail ? `<img src="${esc(previewData.thumbnail)}" alt="GIF preview">` : '')
    : (previewData.media_url
        ? `<video controls autoplay preload="auto" src="${esc(previewData.media_url)}"></video>`
        : (previewData.thumbnail ? `<img src="${esc(previewData.thumbnail)}" alt="${esc(previewData.title || 'Preview')}">` : ''));

  if (!media) return;
  previewModalContent.innerHTML = media;
  previewModal.style.display = 'flex';
  previewModal.offsetHeight;
  previewModal.classList.add('open');
  previewModal.setAttribute('aria-hidden', 'false');

  const video = previewModalContent.querySelector('video');
  if (video) {
    video.play().catch(() => {});
  }
}

function closePreviewModal() {
  if (!previewModal || !previewModal.classList.contains('open')) return;
  const video = previewModalContent.querySelector('video');
  if (video) {
    try { video.pause(); } catch (_) {}
  }
  previewModal.classList.remove('open');
  previewModal.setAttribute('aria-hidden', 'true');
  setTimeout(() => {
    if (!previewModal.classList.contains('open')) {
      previewModal.style.display = 'none';
      previewModalContent.innerHTML = '';
    }
  }, 240);
}

if (previewModal) {
  previewModal.onclick = (e) => {
    if (e.target === previewModal || !previewModalContent.contains(e.target)) {
      closePreviewModal();
    }
  };
}
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && previewModal && previewModal.classList.contains('open')) {
    closePreviewModal();
  }
});

function formatDuration(seconds, rawLabel) {
  let sec = Math.round(Number(seconds) || 0);
  if (!sec && rawLabel) {
    const parts = String(rawLabel).trim().split(':').map(Number);
    if (parts.every(p => !isNaN(p))) {
      if (parts.length === 1) sec = parts[0];
      else if (parts.length === 2) sec = parts[0] * 60 + parts[1];
      else if (parts.length === 3) sec = parts[0] * 3600 + parts[1] * 60 + parts[2];
    }
  }
  if (!sec || sec <= 0) {
    if (!rawLabel) return '';
    const str = String(rawLabel).trim();
    return str.endsWith('s') || str.endsWith('m') ? str : `${str}s`;
  }
  if (sec < 60) {
    return `${sec}s`;
  }
  const mins = Math.floor(sec / 60);
  const remSec = sec % 60;
  if (mins < 60) {
    return remSec > 0 ? `${mins}m ${remSec}s` : `${mins}m`;
  }
  const hrs = Math.floor(mins / 60);
  const remMins = mins % 60;
  return remMins > 0 ? `${hrs}h ${remMins}m` : `${hrs}h`;
}

function renderLocalPreview(data) {
  clearTimeout(collapseAnimTimer);
  if (editorView && editorView.closest('.preview-card')) finishEditorMinimize();
  localEditorData = data;
  previewData = null;
  selectedUrl = '';
  currentMode = 'video';
  const title = data.filename || 'Local video';
  previewContent.innerHTML = `<section class="preview-card">
    <div class="thumb-preview-wrap">
      <div class="thumb-preview-btn" id="openPreviewBtn" role="button" tabindex="0" title="Expand clip view">
        <div class="thumb-media-box"><video src="${esc(data.media_url)}" class="thumb-img local-thumb-video" muted preload="auto" playsinline></video><div class="thumb-play-badge"><svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M8 5v14l11-7z"/></svg></div></div>
        <div class="thumb-meta"><span class="thumb-title">${esc(title)}</span></div>
        <svg class="thumb-expand-icon" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/></svg>
      </div>
    </div>
  </section>`;
  const openBtn = document.getElementById('openPreviewBtn');
  openBtn.onclick = expandPreviewEditor;
  openBtn.onkeydown = event => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      expandPreviewEditor();
    }
  };
  const localThumb = previewContent.querySelector('.local-thumb-video');
  let autoExpandStarted = false;
  const autoExpandLocal = () => {
    if (autoExpandStarted) return;
    autoExpandStarted = true;
    requestAnimationFrame(() => expandPreviewEditor());
  };
  if (localThumb) localThumb.addEventListener('loadeddata', autoExpandLocal, {once:true});
  setTimeout(autoExpandLocal, 240);
  output.offsetHeight;
  requestAnimationFrame(() => output.classList.add('expanded'));
}

function renderPreview(data) {
  clearTimeout(collapseAnimTimer);
  localEditorData = null;
  preparePreviewEditorSource(data);
  const redditGif = data.fallback === 'reddit_gif';
  const canGif = data.seconds <= 30;

  const thumbImg = data.thumbnail
    ? `<img src="${esc(data.thumbnail)}" class="thumb-img" alt="${esc(data.title || 'Preview')}">`
    : `<div class="thumb-placeholder-icon">🎬</div>`;

  const durationText = formatDuration(data.seconds, data.duration);

  let formatOptionsHtml = '';
  if (redditGif) {
    currentMode = 'gif';
    formatOptionsHtml = `
      <section class="format-options" aria-label="Source format">
        <span class="format-label">Source Format:</span>
        <div class="format-pills">
          <button type="button" class="format-pill active" data-mode="gif">Reddit GIF</button>
        </div>
      </section>`;
  } else {
    formatOptionsHtml = `
      <section class="format-options" aria-label="Select format">
        <span class="format-label">Select Format:</span>
        <div class="format-pills">
          <button type="button" class="format-pill ${currentMode === 'video' ? 'active' : ''}" data-mode="video">MP4 Video</button>
          ${canGif ? `<button type="button" class="format-pill ${currentMode === 'gif' ? 'active' : ''}" data-mode="gif">GIF Image</button>` : ''}
          <button type="button" class="format-pill ${currentMode === 'audio' ? 'active' : ''}" data-mode="audio">MP3 Audio</button>
        </div>
      </section>`;
  }

  const thumbBtnHtml = `
    <div class="thumb-preview-wrap">
      <div class="thumb-preview-btn" id="openPreviewBtn" role="button" tabindex="0" title="Expand clip view">
        <div class="thumb-media-box">
          ${thumbImg}
          <div class="thumb-play-badge">
            <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
          </div>
        </div>
        <div class="thumb-meta">
          <span class="thumb-title">${esc(data.title || 'Media Preview')}</span>
          ${durationText ? `<span class="thumb-duration">${esc(durationText)}</span>` : ''}
        </div>
        ${formatOptionsHtml}
        <svg class="thumb-expand-icon" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/>
        </svg>
      </div>
    </div>`;

  previewContent.innerHTML = `<section class="preview-card">${thumbBtnHtml}</section>`;

  const openBtn = document.getElementById('openPreviewBtn');
  if (openBtn) {
    openBtn.onclick = event => {
      if (!event.target.closest('.format-options')) expandPreviewEditor();
    };
    openBtn.onkeydown = event => {
      if ((event.key === 'Enter' || event.key === ' ') && !event.target.closest('.format-options')) {
        event.preventDefault();
        expandPreviewEditor();
      }
    };
  }

  previewContent.querySelectorAll('.format-pill').forEach(pill => {
    pill.onclick = event => {
      event.stopPropagation();
      previewContent.querySelectorAll('.format-pill').forEach(p => p.classList.remove('active'));
      pill.classList.add('active');
      currentMode = pill.dataset.mode;
    };
  });

  const formatOptions = previewContent.querySelector('.format-options');
  if (formatOptions) {
    // Keep the entire padded area around the selector from activating preview.
    formatOptions.onclick = event => event.stopPropagation();
  }

  output.offsetHeight;
  requestAnimationFrame(() => {
    output.classList.add('expanded');
  });
}

async function chooseFormats() {
  const candidate = input.value.trim();
  if (!candidate) return;
  setBusy();
  if (output && output.classList.contains('expanded')) {
    collapsePreview();
  }
  try {
    const response = await fetch('/api/preview', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url: candidate})
    });
    const data = await response.json();
    if (!response.ok) {
      showError(data.code || 'API400', data.error || 'Could not inspect this link.');
      return;
    }
    selectedUrl = candidate;
    previewData = data;
    currentMode = data.fallback === 'reddit_gif' ? 'gif' : 'video';
    input.value = '';
    update();
    renderPreview(data);
  } catch (_) {
    showError('NET001', 'The downloader could not be reached.');
  } finally {
    update();
  }
}

async function performDownload(mode, title, folderPath = null, chipElement = null) {
  closePreviewModal();
  setChipLoading(chipElement);
  setBusy();

  try {
    const payload = {url: selectedUrl, mode, title};
    if (folderPath) payload.folder = folderPath;

    const response = await fetch('/api/download', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    const data = await response.json();
    if (!response.ok) {
      clearChipHud(chipElement);
      if (data.error !== 'Save cancelled.') {
        showError(data.code || 'API400', data.error || 'Download could not start.');
      }
      return;
    }

    activeJobUrl = selectedUrl;
    startStatusPolling(chipElement);
  } catch (_) {
    clearChipHud(chipElement);
    showError('NET001', 'The downloader could not be reached.');
  } finally {
    update();
  }
}

function startStatusPolling(chipElement) {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const res = await fetch('/api/status');
      const data = await res.json();
      const job = data.jobs.find(j => j.url === activeJobUrl);
      if (!job) return;

      if (job.status === 'complete') {
        clearInterval(pollTimer);
        pollTimer = null;
        setChipSuccess(chipElement);
      } else if (job.status === 'failed') {
        clearInterval(pollTimer);
        pollTimer = null;
        clearChipHud(chipElement);
        showError(job.code || 'DL999', job.error || 'Download failed.', job.log);
      }
    } catch (_) {}
  }, 1000);
}

async function handleFolderClick(folderPath, folderName, chipElement) {
  if (editorEditing && editorUploadId && !editorView.hidden && !editorWorkspace.hidden) {
    await startEditorExport(folderPath, chipElement);
    return;
  }
  let targetUrl = selectedUrl;
  let targetTitle = previewData ? previewData.title : 'download';
  let targetMode = currentMode;

  setChipLoading(chipElement);

  if (!targetUrl) {
    const candidate = input.value.trim();
    if (candidate) {
      targetUrl = candidate;
    } else {
      try {
        const clip = await fetch('/api/clipboard').then(r => r.json());
        if (clip.text && /^https?:\/\//i.test(clip.text.trim())) {
          targetUrl = clip.text.trim();
          input.value = targetUrl;
          update();
        }
      } catch (_) {}
    }
    if (!targetUrl) {
      clearChipHud(chipElement);
      input.focus();
      return;
    }

    setBusy();
    try {
      const response = await fetch('/api/preview', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({url: targetUrl})
      });
      const data = await response.json();
      if (!response.ok) {
        clearChipHud(chipElement);
        showError(data.code || 'API400', data.error || 'Could not inspect this link.');
        return;
      }
      selectedUrl = targetUrl;
      previewData = data;
      targetTitle = data.title;
      targetMode = data.fallback === 'reddit_gif' ? 'gif' : 'video';
      currentMode = targetMode;
      input.value = '';
      update();
      renderPreview(data);
    } catch (_) {
      clearChipHud(chipElement);
      showError('NET001', 'The downloader could not be reached.');
      return;
    } finally {
      update();
    }
  }

  await performDownload(targetMode, targetTitle, folderPath, chipElement);
}

async function saveToCustomFolder(chipElement = null) {
  if (editorEditing && editorUploadId && !editorView.hidden && !editorWorkspace.hidden) {
    await startEditorExport(null, chipElement);
    return;
  }
  setChipLoading(chipElement);

  if (!selectedUrl) {
    const candidate = input.value.trim();
    if (candidate) {
      await chooseFormats();
    } else {
      try {
        const clip = await fetch('/api/clipboard').then(r => r.json());
        if (clip.text && /^https?:\/\//i.test(clip.text.trim())) {
          input.value = clip.text.trim();
          update();
          await chooseFormats();
        } else {
          clearChipHud(chipElement);
          input.focus();
          return;
        }
      } catch (_) {
        clearChipHud(chipElement);
        input.focus();
        return;
      }
    }
    if (!selectedUrl) {
      clearChipHud(chipElement);
      return;
    }
  }
  await performDownload(currentMode, previewData ? previewData.title : 'download', null, chipElement);
}

button.onclick = async () => {
  const manual = input.value.trim();
  if (manual) {
    await chooseFormats();
    return;
  }
  setBusy();
  let text = '';
  try {
    const data = await fetch('/api/clipboard').then(r => r.json());
    text = (data.text || '').trim();
    if (!text) {
      showError('CLIP001', 'The clipboard is empty or could not be read.');
      update();
      return;
    }
  } catch {
    showError('CLIP001', 'The Windows clipboard could not be read.');
    update();
    return;
  }

  input.value = text;
  await chooseFormats();
};

input.addEventListener('keydown', e => {
  if (e.key === 'Enter') {
    e.preventDefault();
    if (!button.disabled) button.click();
  }
});

input.addEventListener('input', () => {
  if (output && output.classList.contains('expanded')) {
    collapsePreview();
  } else {
    selectedUrl = '';
    previewData = null;
  }
  update();
});

document.getElementById('closeError').onclick = () => dialog.close();
document.getElementById('copyError').onclick = copyError;

window.addEventListener('resize', () => {
  if (!folderAnimTimer) renderFolders();
  if (editorUploadId && !editorWorkspace.hidden) updateEditorPreview();
});

function seconds(value) {
  return Math.max(0, Math.min(editorDuration || 0, Number(value) || 0));
}

function updateTrim(source) {
  let start = seconds(source === trimStartNumber ? trimStartNumber.value : trimStartRange.value);
  let end = seconds(source === trimEndNumber ? trimEndNumber.value : trimEndRange.value);
  const minimumGap = 1 / (editorFrameRate || 30);
  if (source === trimStartNumber || source === trimStartRange) {
    start = Math.min(start, Math.max(0, end - minimumGap));
    editorVideo.currentTime = start;
  } else {
    end = Math.max(end, Math.min(editorDuration, start + minimumGap));
    editorVideo.currentTime = Math.max(start, end - minimumGap);
  }
  trimStartNumber.value = trimStartRange.value = start.toFixed(6);
  trimEndNumber.value = trimEndRange.value = end.toFixed(6);
  updateClipTimeline();
}

function updateClipTimeline() {
  if (!editorDuration) return;
  const start = Number(trimStartNumber.value) || 0;
  const end = Number(trimEndNumber.value) || editorDuration;
  const startPercent = start / editorDuration * 100;
  const endPercent = end / editorDuration * 100;
  clipStartHandle.style.left = `${startPercent}%`;
  clipEndHandle.style.left = `${endPercent}%`;
  clipSelection.style.left = `${startPercent}%`;
  clipSelection.style.width = `${Math.max(0, endPercent - startPercent)}%`;
  clipStartLabel.textContent = `${start.toFixed(3)}s`;
  clipEndLabel.textContent = `${end.toFixed(3)}s`;
  clipDurationLabel.textContent = `${(end - start).toFixed(3)}s clip`;
}

function setClipBoundary(kind, time = editorVideo.currentTime) {
  clipReplayActive = false;
  clearTimeout(clipReplayTimer);
  const minimumGap = 1 / (editorFrameRate || 30);
  let start = Number(trimStartNumber.value) || 0;
  let end = Number(trimEndNumber.value) || editorDuration;
  if (kind === 'start') {
    start = Math.max(0, Math.min(time, end - minimumGap));
  } else {
    end = Math.min(editorDuration, Math.max(time, start + minimumGap));
  }
  trimStartNumber.value = trimStartRange.value = start.toFixed(6);
  trimEndNumber.value = trimEndRange.value = end.toFixed(6);
  updateClipTimeline();
  const boundaryTime = kind === 'start' ? start : end;
  editorStatus.textContent = `${kind === 'start' ? 'Clip starts' : 'Clip ends'} at ${boundaryTime.toFixed(3)}s`;
}

function stepEditorFrame(direction) {
  clipReplayActive = false;
  clearTimeout(clipReplayTimer);
  editorVideo.pause();
  const start = Number(trimStartNumber.value) || 0;
  const end = Number(trimEndNumber.value) || editorDuration;
  const currentFrame = Math.round(editorVideo.currentTime * editorFrameRate);
  const targetFrame = currentFrame + direction;
  const targetTime = Math.max(start, Math.min(end, targetFrame / editorFrameRate));
  editorVideo.currentTime = targetTime;
  editorStatus.textContent = `Frame ${Math.round(targetTime * editorFrameRate)} · ${targetTime.toFixed(3)}s`;
}

function updatePlaybackButton() {
  const paused = editorVideo.paused;
  playPause.setAttribute('aria-label', paused ? 'Play' : 'Pause');
  playPause.title = paused ? 'Play' : 'Pause';
  playPauseIcon.innerHTML = paused ? '<path d="M8 5v14l11-7z"/>' : '<path d="M7 5h4v14H7zM13 5h4v14h-4z"/>';
}

function toggleEditorPlayback() {
  clipReplayActive = false;
  clearTimeout(clipReplayTimer);
  if (!editorVideo.paused) {
    editorVideo.pause();
    return;
  }
  const start = Number(trimStartNumber.value) || 0;
  const end = Number(trimEndNumber.value) || editorDuration;
  if (editorVideo.currentTime < start || editorVideo.currentTime >= end) editorVideo.currentTime = start;
  editorVideo.play();
}

function finishClipReplay() {
  if (!clipReplayActive) return;
  clipReplayActive = false;
  clearTimeout(clipReplayTimer);
  const clipEnd = Number(trimEndNumber.value) || editorDuration;
  editorVideo.pause();
  editorVideo.currentTime = clipEnd;
}

function monitorClipReplay(_now, metadata) {
  if (!clipReplayActive) return;
  const clipEnd = Number(trimEndNumber.value) || editorDuration;
  const halfFrame = 0.5 / (editorFrameRate || 30);
  if (metadata.mediaTime >= clipEnd - halfFrame) {
    finishClipReplay();
    return;
  }
  if (editorVideo.requestVideoFrameCallback) editorVideo.requestVideoFrameCallback(monitorClipReplay);
}

function replaySelectedClip() {
  const clipStart = Number(trimStartNumber.value) || 0;
  const clipEnd = Number(trimEndNumber.value) || editorDuration;
  editorVideo.pause();
  clearTimeout(clipReplayTimer);
  clipReplayActive = true;
  const beginPlayback = () => {
    if (!clipReplayActive) return;
    editorVideo.play();
    clipReplayTimer = setTimeout(finishClipReplay, Math.max(0, (clipEnd - clipStart) / editorVideo.playbackRate * 1000 + 80));
    if (editorVideo.requestVideoFrameCallback) editorVideo.requestVideoFrameCallback(monitorClipReplay);
  };
  if (Math.abs(editorVideo.currentTime - clipStart) > 0.0005) {
    editorVideo.addEventListener('seeked', beginPlayback, {once:true});
    editorVideo.currentTime = clipStart;
  } else {
    editorVideo.currentTime = clipStart;
    beginPlayback();
  }
}

function readableAspectRatio(ratio) {
  const common = [
    [1, '1:1'], [4 / 5, '4:5'], [3 / 4, '3:4'], [9 / 16, '9:16'],
    [3 / 2, '3:2'], [4 / 3, '4:3'], [16 / 9, '16:9'], [21 / 9, '21:9']
  ];
  const match = common.find(([value]) => Math.abs(value - ratio) / value < 0.015);
  if (match) return match[1];
  const value = ratio >= 1 ? ratio : 1 / ratio;
  return ratio >= 1 ? `${value.toFixed(2)}:1` : `1:${value.toFixed(2)}`;
}

function currentCropAspectLabel() {
  const sourceRatio = (editorVideo.videoWidth && editorVideo.videoHeight) ? editorVideo.videoWidth / editorVideo.videoHeight : 16 / 9;
  return readableAspectRatio(editorCrop.width * sourceRatio / editorCrop.height);
}

function refreshAspectLabels() {
  const freeOption = aspectSelect.querySelector('option[value="free"]');
  const originalOption = aspectSelect.querySelector('option[value="original"]');
  const sourceRatio = (editorVideo.videoWidth && editorVideo.videoHeight) ? editorVideo.videoWidth / editorVideo.videoHeight : 16 / 9;
  if (freeOption) freeOption.textContent = currentCropAspectLabel();
  if (originalOption) originalOption.textContent = readableAspectRatio(sourceRatio);
}

function cropAspectLabel() {
  if (editorAspect === 'free') return currentCropAspectLabel();
  if (editorAspect === 'original') {
    const sourceRatio = (editorVideo.videoWidth && editorVideo.videoHeight) ? editorVideo.videoWidth / editorVideo.videoHeight : 16 / 9;
    return readableAspectRatio(sourceRatio);
  }
  return editorAspect;
}

function updateEditorPreview() {
  const sourceRatio = (editorVideo.videoWidth && editorVideo.videoHeight) ? editorVideo.videoWidth / editorVideo.videoHeight : 16 / 9;
  const outputRatio = editorCrop.width * sourceRatio / editorCrop.height;
  const frameRatio = cropMode ? sourceRatio : outputRatio;
  const availableWidth = Math.max(1, editorWorkspace.clientWidth || 760);
  const frameHeight = Math.min(430, availableWidth / frameRatio);
  refreshAspectLabels();
  editorPreviewFrame.style.aspectRatio = 'auto';
  editorPreviewFrame.style.width = `${Math.round(frameHeight * frameRatio)}px`;
  editorPreviewFrame.style.height = `${Math.round(frameHeight)}px`;
  editorPreviewFrame.style.maxWidth = '100%';
  editorPreviewFrame.style.marginInline = 'auto';
  if (cropMode) {
    editorVideo.style.left = '0';
    editorVideo.style.top = '0';
    editorVideo.style.width = '100%';
    editorVideo.style.height = '100%';
    cropOverlay.hidden = false;
    renderCropBox();
  } else {
    cropOverlay.hidden = true;
    editorVideo.style.left = `${-editorCrop.x / editorCrop.width * 100}%`;
    editorVideo.style.top = `${-editorCrop.y / editorCrop.height * 100}%`;
    editorVideo.style.width = `${100 / editorCrop.width}%`;
    editorVideo.style.height = `${100 / editorCrop.height}%`;
  }
  cropBadge.textContent = `${cropAspectLabel()} · ${Math.round(editorVideo.videoWidth * editorCrop.width)}×${Math.round(editorVideo.videoHeight * editorCrop.height)}`;
}

function renderCropBox() {
  refreshAspectLabels();
  cropBox.style.left = `${editorCrop.x * 100}%`;
  cropBox.style.top = `${editorCrop.y * 100}%`;
  cropBox.style.width = `${editorCrop.width * 100}%`;
  cropBox.style.height = `${editorCrop.height * 100}%`;
  cropBox.style.setProperty('--crop-box-width', `${cropBox.offsetWidth}px`);
  cropBox.style.setProperty('--crop-box-height', `${cropBox.offsetHeight}px`);
  cropBadge.textContent = `${cropAspectLabel()} · ${Math.round(editorVideo.videoWidth * editorCrop.width)}×${Math.round(editorVideo.videoHeight * editorCrop.height)}`;
}

function setAspectCrop(aspect) {
  const sourceRatio = (editorVideo.videoWidth && editorVideo.videoHeight) ? editorVideo.videoWidth / editorVideo.videoHeight : 16 / 9;
  const targetRatios = {'1:1': 1, '4:5': 4 / 5, '9:16': 9 / 16, '16:9': 16 / 9};
  if (aspect === 'free') {
    updateEditorPreview();
    return;
  }
  if (aspect === 'original') {
    editorCrop = {x:0, y:0, width:1, height:1};
  } else {
    const normalizedRatio = targetRatios[aspect] / sourceRatio;
    const width = normalizedRatio >= 1 ? 1 : normalizedRatio;
    const height = normalizedRatio >= 1 ? 1 / normalizedRatio : 1;
    const centerX = editorCrop.x + editorCrop.width / 2;
    const centerY = editorCrop.y + editorCrop.height / 2;
    editorCrop = {
      x: Math.max(0, Math.min(1 - width, centerX - width / 2)),
      y: Math.max(0, Math.min(1 - height, centerY - height / 2)),
      width,
      height
    };
  }
  updateEditorPreview();
}

function setCropMode(enabled) {
  cropMode = enabled;
  toggleCrop.classList.toggle('active', cropMode);
  const label = toggleCrop.querySelector('span');
  if (label) label.textContent = cropMode ? 'Done' : 'Crop';
  toggleCrop.setAttribute('aria-label', cropMode ? 'Finish cropping' : 'Adjust crop');
  toggleCrop.title = cropMode ? 'Finish cropping' : 'Adjust crop';
  if (cropMode) editorVideo.pause();
  updateEditorPreview();
}

async function chooseEditorSource() {
  editorUploadId = '';
  openVideoButton.disabled = true;
  editorStatus.textContent = 'Choose a source video…';
  try {
    const response = await fetch('/api/editor/source/choose', {method:'POST'});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not open the video.');
    if (data.cancelled) {
      editorStatus.textContent = '';
      return;
    }
    renderLocalPreview(data);
  } catch (error) {
    editorStatus.textContent = '';
    showError('EDIT001', error.message || String(error));
  } finally {
    openVideoButton.disabled = false;
  }
}

function loadEditorSource(data, sourceName) {
  editorUploadId = data.upload_id;
  editorFrameRate = Number(data.frame_rate) || 30;
  editorSourceName.textContent = sourceName || 'Video editor';
  editorWorkspace.hidden = false;
  editorVideo.src = `${data.media_url}${data.media_url.includes('?') ? '&' : '?'}v=${Date.now()}`;
  editorEditing = true;
  editorStatus.textContent = 'Ready to edit';
  revealEditor();
}

function runEditorMorph(update) {
  if (!document.startViewTransition || window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    update();
    return;
  }
  try {
    document.startViewTransition(update).finished.catch(() => {});
  } catch (_) {
    update();
  }
}

function revealEditor() {
  clearTimeout(editorAnimationTimer);
  const previewCard = editorView.closest('.preview-card');
  editorView.classList.remove('editor-minimizing');
  editorView.classList.add('editor-opening');
  editorView.style.maxHeight = '0px';
  editorView.hidden = false;
  editorView.offsetHeight;
  requestAnimationFrame(() => {
    runEditorMorph(() => {
      if (previewCard) previewCard.classList.add('editor-expanded');
      editorView.style.maxHeight = `${editorView.scrollHeight}px`;
      editorView.classList.remove('editor-opening');
    });
    editorAnimationTimer = setTimeout(() => {
      if (!editorView.classList.contains('editor-minimizing')) editorView.style.maxHeight = 'none';
    }, 520);
  });
}

function preparePreviewEditorSource(data) {
  if (!data || !data.editor_token) return Promise.resolve({error:'This preview cannot be edited.'});
  if (previewEditorToken === data.editor_token && previewEditorPromise) return previewEditorPromise;
  previewEditorToken = data.editor_token;
  previewEditorPromise = fetch('/api/editor/source/from-preview', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({editor_token:data.editor_token, title:data.title || 'Pasted video'})
  }).then(async response => {
    const prepared = await response.json();
    if (!response.ok) throw new Error(prepared.error || 'Could not cache this video for editing.');
    return prepared;
  }).catch(error => ({error:error.message || String(error)}));
  return previewEditorPromise;
}

async function expandPreviewEditor() {
  if (editorUploadId && editorView.closest('.preview-card')) {
    minimizeEditor();
    return;
  }
  if (!localEditorData && (!previewData || !previewData.editor_token)) return;
  const previewCard = previewContent.querySelector('.preview-card');
  if (!previewCard) return;
  try {
    let data;
    let sourceName;
    if (localEditorData) {
      data = localEditorData;
      sourceName = data.filename || 'Local video';
    } else {
      data = await preparePreviewEditorSource(previewData);
      if (data.error) throw new Error(data.error);
      sourceName = previewData.title || 'Pasted video';
    }
    previewCard.appendChild(editorView);
    loadEditorSource(data, sourceName);
  } catch (error) {
    editorHome.appendChild(editorView);
    editorView.hidden = true;
    editorStatus.textContent = '';
    showError('EDIT003', error.message || String(error));
  }
}

function finishEditorMinimize() {
  clearTimeout(editorAnimationTimer);
  const previewCard = editorView.closest('.preview-card');
  if (previewCard) previewCard.classList.remove('editor-expanded');
  editorVideo.pause();
  editorVideo.removeAttribute('src');
  editorVideo.load();
  editorHome.appendChild(editorView);
  editorView.hidden = true;
  editorView.classList.remove('editor-opening', 'editor-minimizing');
  editorView.style.maxHeight = '';
  editorWorkspace.hidden = true;
  editorUploadId = '';
  editorEditing = false;
  setCropMode(false);
}

function minimizeEditor() {
  if (editorView.hidden || editorView.classList.contains('editor-minimizing')) return;
  editorVideo.pause();
  clearTimeout(editorAnimationTimer);
  const previewCard = editorView.closest('.preview-card');
  editorView.style.maxHeight = `${editorView.scrollHeight}px`;
  editorView.offsetHeight;
  runEditorMorph(() => {
    if (previewCard) previewCard.classList.remove('editor-expanded');
    editorView.classList.add('editor-minimizing');
    editorView.style.maxHeight = '0px';
  });
  editorAnimationTimer = setTimeout(finishEditorMinimize, 560);
}

function closeEditorAfterSave() {
  clearTimeout(editorAnimationTimer);
  editorVideo.pause();
  output.classList.add('saving-away');
  output.offsetHeight;
  requestAnimationFrame(() => output.classList.remove('expanded'));
  editorAnimationTimer = setTimeout(() => {
    output.classList.remove('saving-away');
    collapsePreview(true);
  }, 520);
}

function pollEditorExport(jobId, chipElement = null) {
  clearTimeout(editorPollTimer);
  editorPollTimer = setTimeout(async () => {
    try {
      const response = await fetch(`/api/editor/status?id=${encodeURIComponent(jobId)}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Could not check export.');
      if (data.status === 'complete') {
        editorStatus.textContent = `${data.replace_source ? 'Saved' : 'Saved new copy'} ${data.filename}`;
        if (chipElement) setChipSuccess(chipElement);
        await loadFolders();
        closeEditorAfterSave();
        return;
      }
      if (data.status === 'failed') throw new Error(data.error || 'Export failed.');
      editorStatus.textContent = 'Exporting video…';
      pollEditorExport(jobId, chipElement);
    } catch (error) {
      editorStatus.textContent = '';
      if (chipElement) clearChipHud(chipElement);
      showError('EDIT002', error.message || String(error));
    }
  }, 900);
}

openVideoButton.onclick = chooseEditorSource;
editorVideo.onloadedmetadata = () => {
  editorDuration = Number.isFinite(editorVideo.duration) ? editorVideo.duration : 0;
  editorAspect = 'free';
  editorCrop = {x:0, y:0, width:1, height:1};
  cropMode = false;
  toggleCrop.classList.remove('active');
  const cropLabel = toggleCrop.querySelector('span');
  if (cropLabel) cropLabel.textContent = 'Crop';
  aspectSelect.value = 'free';
  [trimStartRange, trimEndRange, trimStartNumber, trimEndNumber].forEach(control => control.max = editorDuration.toFixed(6));
  trimStartNumber.value = trimStartRange.value = '0.000000';
  trimEndNumber.value = trimEndRange.value = editorDuration.toFixed(6);
  updateClipTimeline();
  updateEditorPreview();
};
[trimStartNumber, trimEndNumber, trimStartRange, trimEndRange].forEach(control => control.addEventListener('input', () => updateTrim(control)));
aspectSelect.onchange = () => {
  editorAspect = aspectSelect.value;
  setAspectCrop(editorAspect);
};
toggleCrop.onclick = () => setCropMode(!cropMode);
resetCrop.onclick = () => {
  editorCrop = {x:0, y:0, width:1, height:1};
  editorAspect = 'free';
  aspectSelect.value = 'free';
  updateEditorPreview();
};
editorView.onclick = event => {
  if (event.target === editorView || event.target === editorWorkspace) minimizeEditor();
};
editorVideo.onclick = event => {
  event.stopPropagation();
  if (!cropMode) toggleEditorPlayback();
};
previousFrame.onclick = event => { event.stopPropagation(); stepEditorFrame(-1); };
nextFrame.onclick = event => { event.stopPropagation(); stepEditorFrame(1); };
startClip.onclick = event => { event.stopPropagation(); setClipBoundary('start'); };
endClip.onclick = event => { event.stopPropagation(); setClipBoundary('end'); };
replayClip.onclick = event => {
  event.stopPropagation();
  replaySelectedClip();
};
playPause.onclick = event => { event.stopPropagation(); toggleEditorPlayback(); };
editorVideo.onplay = updatePlaybackButton;
editorVideo.onpause = updatePlaybackButton;
editorVideo.ontimeupdate = () => {
  if (!editorDuration) return;
  clipPlayhead.style.left = `${Math.max(0, Math.min(100, editorVideo.currentTime / editorDuration * 100))}%`;
  const clipEnd = Number(trimEndNumber.value) || editorDuration;
  if (!editorVideo.paused && editorVideo.currentTime >= clipEnd) {
    if (clipReplayActive) finishClipReplay();
    else {
      editorVideo.currentTime = clipEnd;
      editorVideo.pause();
    }
  }
};
clipTimeline.onclick = event => {
  if (event.target.closest('.clip-handle')) return;
  clipReplayActive = false;
  clearTimeout(clipReplayTimer);
  editorVideo.pause();
  const bounds = clipTimeline.getBoundingClientRect();
  editorVideo.currentTime = Math.max(0, Math.min(editorDuration, (event.clientX - bounds.left) / bounds.width * editorDuration));
};
[clipStartHandle, clipEndHandle].forEach(handle => {
  handle.onpointerdown = event => {
    event.preventDefault();
    event.stopPropagation();
    clipPointer = {id:event.pointerId, kind:handle === clipStartHandle ? 'start' : 'end', handle};
    handle.setPointerCapture(event.pointerId);
  };
  handle.onpointermove = event => {
    if (!clipPointer || clipPointer.id !== event.pointerId || clipPointer.handle !== handle) return;
    const bounds = clipTimeline.getBoundingClientRect();
    const time = Math.max(0, Math.min(editorDuration, (event.clientX - bounds.left) / bounds.width * editorDuration));
    editorVideo.currentTime = time;
    setClipBoundary(clipPointer.kind, time);
  };
  handle.onpointerup = handle.onpointercancel = event => {
    if (!clipPointer || clipPointer.id !== event.pointerId || clipPointer.handle !== handle) return;
    clipPointer = null;
    if (handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId);
  };
});
cropOverlay.onpointerdown = event => {
  const handle = event.target.closest('.crop-handle');
  const insideBox = event.target.closest('#cropBox');
  if (!insideBox) return;
  event.preventDefault();
  if (handle) {
    editorAspect = 'free';
    aspectSelect.value = 'free';
  }
  const bounds = cropOverlay.getBoundingClientRect();
  cropPointer = {
    id: event.pointerId,
    mode: handle ? 'resize' : 'move',
    handle: handle ? handle.dataset.handle : '',
    startX: (event.clientX - bounds.left) / bounds.width,
    startY: (event.clientY - bounds.top) / bounds.height,
    crop: {...editorCrop}
  };
  cropOverlay.setPointerCapture(event.pointerId);
};
cropOverlay.onpointermove = event => {
  if (!cropPointer || cropPointer.id !== event.pointerId) return;
  const bounds = cropOverlay.getBoundingClientRect();
  const pointerX = (event.clientX - bounds.left) / bounds.width;
  const pointerY = (event.clientY - bounds.top) / bounds.height;
  const start = cropPointer.crop;
  if (cropPointer.mode === 'move') {
    editorCrop.x = Math.max(0, Math.min(1 - start.width, start.x + pointerX - cropPointer.startX));
    editorCrop.y = Math.max(0, Math.min(1 - start.height, start.y + pointerY - cropPointer.startY));
  } else {
    const direction = cropPointer.handle;
    const minimum = 0.04;
    let left = start.x;
    let right = start.x + start.width;
    let top = start.y;
    let bottom = start.y + start.height;
    if (direction.includes('w')) left = Math.max(0, Math.min(pointerX, right - minimum));
    if (direction.includes('e')) right = Math.min(1, Math.max(pointerX, left + minimum));
    if (direction.includes('n')) top = Math.max(0, Math.min(pointerY, bottom - minimum));
    if (direction.includes('s')) bottom = Math.min(1, Math.max(pointerY, top + minimum));
    editorCrop = {x:left, y:top, width:right - left, height:bottom - top};
  }
  renderCropBox();
};
cropOverlay.onpointerup = cropOverlay.onpointercancel = event => {
  if (!cropPointer || cropPointer.id !== event.pointerId) return;
  cropPointer = null;
  if (cropOverlay.hasPointerCapture(event.pointerId)) cropOverlay.releasePointerCapture(event.pointerId);
  updateEditorPreview();
};
async function startEditorExport(folderPath = null, chipElement = null) {
  if (!editorUploadId) return;
  if (chipElement) setChipLoading(chipElement);
  const saveMode = preserveOriginal.checked ? 'copy' : 'replace';
  editorStatus.textContent = folderPath ? 'Saving edited video to selected folder…' : 'Choose where to save the new video…';
  try {
    const response = await fetch('/api/editor/export', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({
      upload_id: editorUploadId, start: Number(trimStartNumber.value), end: Number(trimEndNumber.value), crop: editorCrop, save_mode: saveMode, folder: folderPath
    })});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not start export.');
    editorStatus.textContent = `Exporting ${data.filename}…`;
    pollEditorExport(data.job_id, chipElement);
  } catch (error) {
    editorStatus.textContent = '';
    if (chipElement) clearChipHud(chipElement);
    if (!/cancelled/i.test(error.message || '')) showError('EDIT002', error.message || String(error));
  }
}

const launchUrl = new URLSearchParams(window.location.search).get('url');
if (launchUrl && /^https?:\/\/\S+$/i.test(launchUrl)) {
  input.value = launchUrl;
  window.history.replaceState({}, '', '/');
}

update();
loadFolders();
</script></body></html>'''

class Handler(BaseHTTPRequestHandler):
    def send_json(self, value: object, status: int=200) -> None:
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_local_video(self, path: Path) -> None:
        size = path.stat().st_size
        start, end = 0, max(0, size - 1)
        status = 200
        if byte_range := self.headers.get("Range"):
            match = re.match(r"bytes=(\d*)-(\d*)", byte_range)
            if match:
                if match.group(1): start = int(match.group(1))
                if match.group(2): end = min(end, int(match.group(2)))
                if start > end or start >= size:
                    self.send_error(416); return
                status = 206
        content_length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "video/mp4")
        self.send_header("Content-Length", str(content_length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with path.open("rb") as source:
                source.seek(start)
                remaining = content_length
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk: break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (OSError, BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:
        parsed_path = urlsplit(self.path)
        if parsed_path.path == "/api/editor/status":
            job_id = parse_qs(parsed_path.query).get("id", [""])[0]
            with EDITOR_LOCK:
                job = dict(EDITOR_JOBS.get(job_id, {}))
            if not job:
                self.send_json({"error": "Export job not found."}, 404)
            else:
                self.send_json({key: value for key, value in job.items() if key != "upload_id"})
            return
        if parsed_path.path.startswith("/api/editor/source/"):
            upload_id = parsed_path.path.rsplit("/", 1)[-1]
            with EDITOR_LOCK:
                upload = EDITOR_UPLOADS.get(upload_id)
            path = Path(str(upload["path"])) if upload else None
            if not path or not path.is_file():
                self.send_error(404); return
            self.send_local_video(path)
            return
        if self.path == "/api/app-status":
            self.send_json({"app": "media-downloader"})
            return
        if self.path == "/api/status":
            with LOCK: self.send_json({"jobs": list(reversed(JOBS))})
            return
        if self.path == "/api/clipboard":
            self.send_json({"text": clipboard_text()})
            return
        if self.path == "/api/folders":
            self.send_json(get_folders_summary())
            return
        if self.path.startswith("/api/stream/"):
            item = PREVIEWS.get(self.path.rsplit("/", 1)[-1])
            if not item or not item["url"]:
                self.send_error(404); return
            headers = dict(item["headers"])
            if byte_range := self.headers.get("Range"): headers["Range"] = byte_range
            try:
                with urlopen(Request(str(item["url"]), headers=headers), timeout=30) as source:
                    self.send_response(getattr(source, "status", 200))
                    for header in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                        if value := source.headers.get(header): self.send_header(header, value)
                    self.end_headers()
                    while chunk := source.read(65536): self.wfile.write(chunk)
            except (OSError, BrokenPipeError):
                pass
            return
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(PAGE.encode())

    def do_POST(self) -> None:
        try:
            if self.path == "/api/editor/source/choose":
                root = Tk(); root.withdraw(); root.attributes("-topmost", True)
                chosen = filedialog.askopenfilename(
                    title="Choose a video to edit",
                    filetypes=[("Video files", "*.mp4 *.mov *.mkv *.webm *.m4v *.avi"), ("All files", "*.*")],
                )
                root.destroy()
                if not chosen:
                    self.send_json({"cancelled": True}); return
                source_path = Path(chosen).resolve()
                if not source_path.is_file() or source_path.suffix.lower() not in EDITOR_EXTENSIONS:
                    raise ValueError("Choose an MP4, MOV, MKV, WebM, M4V, or AVI video.")
                upload_id = secrets.token_urlsafe(18)
                with EDITOR_LOCK:
                    EDITOR_UPLOADS[upload_id] = {
                        "path": str(source_path), "original_path": str(source_path), "created": time.time(), "owned_temp": False,
                    }
                self.send_json({
                    "upload_id": upload_id, "filename": source_path.name,
                    "media_url": f"/api/editor/source/{upload_id}", "frame_rate": probe_video_frame_rate(source_path),
                })
                return

            if self.path == "/api/editor/upload":
                content_length = int(self.headers.get("Content-Length", 0))
                if content_length <= 0:
                    raise ValueError("Choose a video to upload.")
                extension = self.headers.get("X-File-Extension", ".mp4").lower()
                if extension not in EDITOR_EXTENSIONS:
                    raise ValueError("Choose an MP4, MOV, MKV, WebM, M4V, or AVI video.")
                upload_id = secrets.token_urlsafe(18)
                EDITOR_TEMP.mkdir(parents=True, exist_ok=True)
                target = EDITOR_TEMP / f"{upload_id}{extension}"
                remaining = content_length
                with target.open("wb") as destination:
                    while remaining:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError("The video upload ended unexpectedly.")
                        destination.write(chunk)
                        remaining -= len(chunk)
                with EDITOR_LOCK:
                    EDITOR_UPLOADS[upload_id] = {"path": str(target), "created": time.time()}
                self.send_json({"upload_id": upload_id, "frame_rate": probe_video_frame_rate(target)})
                return

            if self.path == "/api/editor/source/from-preview":
                content_length = int(self.headers.get("Content-Length", 0))
                raw_data = self.rfile.read(content_length) if content_length > 0 else b"{}"
                request_data = json.loads(raw_data.decode("utf-8") if raw_data else "{}")
                token = str(request_data.get("editor_token", ""))
                preview_item = PREVIEWS.get(token)
                if not preview_item:
                    raise ValueError("This pasted video preview is no longer available. Load the link again.")
                upload_id = secrets.token_urlsafe(18)
                cached_value = str(preview_item.get("cached_path") or "")
                local_source = Path(cached_value) if cached_value else EDITOR_TEMP / f"preview-{token}.mp4"
                if not local_source.is_file():
                    page_url = str(preview_item.get("page_url") or "")
                    provider = provider_for(page_url)
                    if not provider:
                        raise ValueError("The pasted video source is no longer supported.")
                    EDITOR_TEMP.mkdir(parents=True, exist_ok=True)
                    materialize_job = {
                        "url": page_url, "mode": "video", "path": str(local_source), "status": "downloading", "log": "",
                        "strategy": str(preview_item.get("strategy") or "download"),
                    }
                    if preview_item.get("url"):
                        if preview_item.get("kind") == "gif" and preview_item.get("direct"):
                            materialize_job["direct_url"] = str(preview_item["url"])
                        else:
                            materialize_job["direct_media_url"] = str(preview_item["url"])
                            materialize_job["direct_headers"] = json.dumps(preview_item.get("headers") or {})
                    download(materialize_job, provider)
                    if materialize_job.get("status") != "complete" or not local_source.is_file():
                        raise ValueError(str(materialize_job.get("error") or "The pasted video could not be prepared for editing."))
                    preview_item["cached_path"] = str(local_source)
                upload_record = {
                    "path": str(local_source), "created": time.time(), "owned_temp": False, "cached_source": True,
                }
                editor_media_url = f"/api/editor/source/{upload_id}"
                frame_rate = probe_video_frame_rate(local_source)
                with EDITOR_LOCK:
                    EDITOR_UPLOADS[upload_id] = upload_record
                self.send_json({
                    "upload_id": upload_id,
                    "filename": str(request_data.get("title") or "Pasted video"),
                    "media_url": editor_media_url,
                    "frame_rate": frame_rate,
                    "remote_source": True,
                })
                return

            content_length = int(self.headers.get("Content-Length", 0))
            raw_data = self.rfile.read(content_length) if content_length > 0 else b"{}"
            data = json.loads(raw_data.decode("utf-8") if raw_data else "{}")

            if self.path == "/api/editor/export":
                upload_id = str(data.get("upload_id", ""))
                with EDITOR_LOCK:
                    upload = EDITOR_UPLOADS.get(upload_id)
                local_path = Path(str(upload.get("path", ""))) if upload else None
                if not upload or not ((local_path and local_path.is_file()) or upload.get("source_url")):
                    raise ValueError("Upload a video before exporting.")
                start = float(data.get("start", 0))
                end = float(data.get("end", 0))
                if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
                    raise ValueError("The trim end must be after the trim start.")
                crop = data.get("crop") or {}
                crop_x = float(crop.get("x", 0))
                crop_y = float(crop.get("y", 0))
                crop_width = float(crop.get("width", 1))
                crop_height = float(crop.get("height", 1))
                editor_crop_filter(crop_x, crop_y, crop_width, crop_height)
                save_mode = str(data.get("save_mode", "copy"))
                if save_mode not in {"replace", "copy"}:
                    raise ValueError("Unknown save mode.")
                folder_value = str(data.get("folder") or "").strip()
                if folder_value:
                    target_folder = Path(folder_value).resolve()
                    final_path = editor_quick_save_path(target_folder)
                    record_folder_use(str(target_folder))
                else:
                    final_path = Path(choose_editor_path()).resolve()
                replace_source = editor_should_replace_source(save_mode, upload)
                if replace_source:
                    original_value = upload.get("original_path")
                    if not original_value:
                        raise ValueError("Choose the source video again before replacing it.")
                    original_path = Path(str(original_value)).resolve()
                    temporary_path, final_path = editor_replace_paths(original_path, final_path)
                    path = str(temporary_path)
                else:
                    original_value = upload.get("original_path")
                    if original_value and Path(str(original_value)).resolve() == final_path:
                        raise ValueError("Choose a different filename to preserve the original source video.")
                    path = str(final_path)
                job_id = secrets.token_urlsafe(18)
                job: dict[str, object] = {
                    "id": job_id, "upload_id": upload_id, "path": path, "filename": Path(path).name,
                    "status": "exporting", "start": start, "end": end,
                    "crop_x": crop_x, "crop_y": crop_y, "crop_width": crop_width, "crop_height": crop_height,
                    "replace_source": replace_source, "final_path": str(final_path),
                }
                with EDITOR_LOCK:
                    EDITOR_JOBS[job_id] = job
                threading.Thread(target=export_edited_video, args=(job_id,), daemon=True).start()
                self.send_json({"job_id": job_id, "filename": final_path.name, "save_mode": save_mode})
                return

            if self.path == "/api/folders/add":
                root = Tk(); root.withdraw(); root.attributes("-topmost", True)
                chosen = filedialog.askdirectory(title="Choose folder for Quick Save")
                root.destroy()
                if chosen and Path(chosen).is_dir():
                    record_folder_use(str(Path(chosen)))
                self.send_json(get_folders_summary())
                return

            if self.path == "/api/folders/remove":
                folder = data.get("folder")
                if folder:
                    remove_folder(str(folder))
                self.send_json(get_folders_summary())
                return

            if self.path == "/api/open-folder":
                target = data.get("folder")
                if target and Path(target).is_dir():
                    subprocess.Popen(["explorer", str(Path(target).resolve())], creationflags=NO_WINDOW)
                    self.send_json({"ok": True})
                else:
                    self.send_json({"error": "Folder not found"}, 400)
                return

            url = str(data.get("url", "")).strip()
            if not re.match(r"^https?://", url): raise ValueError("Please enter a valid http(s) link.")
            provider = provider_for(url)
            if not provider: raise ValueError("Please enter a valid public http(s) page link.")
            if self.path == "/api/preview": self.send_json(preview(url)); return
            if self.path != "/api/download": self.send_json({"error": "Not found"}, 404); return
            mode = str(data.get("mode", "video"))
            if mode not in {"video", "gif", "audio"}: raise ValueError("Unknown download format.")
            direct_url = DIRECT_MEDIA.get(url)
            if direct_url and mode == "audio": raise ValueError("GIFs do not contain audio; choose Save GIF or MP4.")

            folder = data.get("folder")
            if folder:
                target_dir = Path(folder)
                if not target_dir.is_dir() or is_restricted_folder(target_dir):
                    raise ValueError(f"Folder '{folder}' does not exist or is not permitted.")
                path = str(generate_unique_path(target_dir, mode))
                record_folder_use(str(target_dir))
            else:
                path = choose_path(str(data.get("title", "download")), mode)

            job = {"url": url, "mode": mode, "path": path, "status": "downloading", "log": ""}
            job["strategy"] = RESOLUTION_STRATEGIES.get(url, "download")
            if direct_url:
                job["direct_url"] = direct_url
            else:
                candidate = RESOLVED_CANDIDATES.get(url)
                if candidate and candidate.direct and candidate.source_url:
                    job["direct_media_url"] = candidate.source_url
                    job["direct_headers"] = json.dumps(candidate.headers)
            with LOCK: JOBS.append(job)
            threading.Thread(target=download, args=(job, provider), daemon=True).start()
            self.send_json({
                "message": f"Started download to {path}.",
                "path": path,
                "folder": str(Path(path).parent),
                "filename": Path(path).name,
                "mode": mode,
            })
        except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
            self.send_json({"error": str(exc)}, 400)

    def log_message(self, *_: object) -> None: pass

if __name__ == "__main__":
    launch_url = protocol_launch_url()
    background = "--background" in sys.argv[1:]
    if existing_address := running_instance_address(fixed_only=background):
        if not background:
            webbrowser.open_new_tab(browser_address(existing_address, launch_url))
        raise SystemExit(0)

    try:
        server = ThreadingHTTPServer(("127.0.0.1", APP_PORT), Handler)
    except OSError:
        if background:
            raise SystemExit(1)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    address = f"http://127.0.0.1:{server.server_port}"
    try:
        INSTANCE_FILE.write_text(json.dumps({"port": server.server_port}), encoding="utf-8")
    except OSError:
        pass
    if sys.stdout is not None:
        try:
            print(f"Open {address}", flush=True)
        except Exception:
            pass
    if not background:
        threading.Timer(.4, lambda: webbrowser.open_new_tab(browser_address(address, launch_url))).start()
    server.serve_forever()
