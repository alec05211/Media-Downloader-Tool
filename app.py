"""Local downloader for public media the user is permitted to save."""
from __future__ import annotations
import json, re, secrets, shutil, string, subprocess, sys, threading, time, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import Tk, filedialog
from urllib.request import Request, urlopen

from media_downloader.diagnostics import record_download_failure, record_resolution
from media_downloader.pipeline import ProviderAdapter, resolve
from media_downloader.providers import RedditProvider, YtDlpProvider

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
FOLDER_LOCK = threading.Lock()

PREVIEWS: dict[str, dict[str, object]] = {}
DIRECT_MEDIA: dict[str, str] = {}
RESOLUTION_STRATEGIES: dict[str, str] = {}
PROVIDERS: tuple[ProviderAdapter, ...] = (
    YtDlpProvider("YouTube", ("youtube.com", "youtu.be"), ROOT),
    YtDlpProvider("TikTok", ("tiktok.com",), ROOT),
    YtDlpProvider("Instagram", ("instagram.com",), ROOT),
    YtDlpProvider("X", ("x.com", "twitter.com"), ROOT),
    RedditProvider(ROOT),
)
JOBS: list[dict[str,str]] = []; LOCK = threading.Lock()

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
    frequent = sorted(valid, key=lambda x: (x["count"], x["last_used"]), reverse=True)[:8]
    recent = sorted(valid, key=lambda x: (x["last_used"], x["count"]), reverse=True)[:8]
    return {"frequent": frequent, "recent": recent}

def generate_unique_path(folder: Path, mode: str) -> Path:
    suffix = {"gif": ".gif", "audio": ".mp3"}.get(mode, ".mp4")
    for _ in range(500):
        name = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12)) + suffix
        target = folder / name
        if not target.exists():
            return target
    return folder / f"download_{int(time.time())}{suffix}"

def choose_path(title: str, mode: str) -> str:
    suffix = {"gif": ".gif", "audio": ".mp3"}.get(mode, ".mp4")
    name = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12)) + suffix
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

def ffmpeg_binary() -> str | None:
    if installed := shutil.which("ffmpeg"): return installed
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError): return None

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
    if "requested format is not available" in value or "no video formats" in value: return "FMT001", "No single progressive MP4 stream was exposed by the source. The app intentionally avoids separate audio/video files."
    if "unable to download" in value or "timed out" in value or "connection" in value: return "NET001", "The source could not be reached or stopped responding. Check your connection and retry."
    if "permission denied" in value or "access is denied" in value: return "FS001", "Windows denied permission to write to the selected destination. Choose another folder or adjust its permissions."
    if "video unavailable" in value or "this video is not available" in value or "does not exist" in value: return "SRC002", "The source video is unavailable, removed, or the link is no longer valid."
    return "DL999", "The downloader returned an unclassified error. Hover for the original diagnostic output."

def preview(url: str) -> dict[str, object]:
    provider = provider_for(url)
    if not provider:
        raise ValueError("Supported sources: YouTube, TikTok, Instagram, X, and Reddit.")
    result = resolve(provider, url)
    record_resolution(ROOT, url, result)
    candidate = result.candidate
    if not candidate:
        details = "; ".join(f"{attempt.strategy}: {attempt.detail}" for attempt in result.attempts)
        raise ValueError(f"No public media could be resolved. Tried: {details}")
    RESOLUTION_STRATEGIES[url] = result.attempts[-1].strategy
    if candidate.direct:
        DIRECT_MEDIA[url] = candidate.source_url
    if candidate.source_url and not candidate.direct:
        token = secrets.token_urlsafe(18)
        PREVIEWS[token] = {"url": candidate.source_url, "headers": candidate.headers}
        media_url = f"/api/stream/{token}"
    else:
        media_url = ""
    return {"title": candidate.title, "thumbnail": candidate.thumbnail_url, "media_url": media_url,
            "duration": candidate.duration_label, "seconds": candidate.duration_seconds,
            "resolution": candidate.resolution, "fallback": "reddit_gif" if candidate.kind == "gif" else ""}

def download(job: dict[str,str], provider: ProviderAdapter) -> None:
    try:
        if direct_url := job.get("direct_url"):
            request = Request(direct_url, headers={"User-Agent": "Media-Downloader-Tool/1.0 (public GIF download)", "Referer": job["url"]})
            try:
                with urlopen(request, timeout=45) as source, open(job["path"], "wb") as destination:
                    shutil.copyfileobj(source, destination)
            except OSError as exc:
                raise RuntimeError(f"Reddit direct GIF download failed: {exc}") from exc
            job["log"] = "Downloaded public Reddit GIF directly."
            job["status"] = "complete"
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
        options: dict[str, object] = {"noplaylist": True, "no_warnings": True, "quiet": True,
                                      "format": "best[ext=mp4]", "outtmpl": output_template,
                                      "logger": logger}
        if ffmpeg: options["ffmpeg_location"] = ffmpeg
        with YoutubeDL(options) as downloader:
            result = downloader.download([job["url"]])
        output = "\n".join(logger.messages)
        returncode = int(result or 0)
        if returncode == 0 and job["mode"] in {"gif", "audio"}:
            intermediate = str(Path(job["path"]).with_suffix(".mp4"))
            conversion_args = ([ffmpeg, "-y", "-i", intermediate, "-vf", "fps=12,scale=640:-1:flags=lanczos", job["path"]]
                                if job["mode"] == "gif" else [ffmpeg, "-y", "-i", intermediate, "-vn", "-codec:a", "libmp3lame", "-q:a", "2", job["path"]])
            conversion = subprocess.run(
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
body{font-family:system-ui,sans-serif;max-width:780px;margin:48px auto;background:#10131a;color:#eef2ff;padding:0 20px}
h1{margin-bottom:20px}
form{background:#1c2230;border:1px solid #2e3950;border-radius:10px;padding:18px;margin:20px 0}
.entry{display:flex}
.entry input{min-width:0;flex:1;box-sizing:border-box;padding:12px;border-radius:6px 0 0 6px;border:1px solid #4b5b7c;background:#111722;color:#fff;font-size:15px}
.entry input:focus,.entry input:focus-visible{outline:none !important;box-shadow:none !important;border-color:#4b5b7c !important}
.entry button{display:inline-flex;align-items:center;justify-content:center;gap:6px;min-width:52px;border:0;font-weight:700;cursor:pointer;margin:0;border-radius:0 6px 6px 0;padding:10px 18px;background:#76a7ff;color:#10131a;font-size:14px;transition:background .15s ease,color .15s ease,min-width .15s ease}
.entry button svg{display:block;pointer-events:none}
.entry button:focus,.entry button:focus-visible{outline:none !important;box-shadow:none !important}
.entry button:disabled{background:#526174;cursor:wait;color:#cbd5e1}

/* Format Pills */
.format-options{display:flex;justify-content:center;align-items:center;flex-wrap:wrap;gap:10px;margin:18px auto}
.format-label{font-size:13px;font-weight:600;color:#aab4cd}
.format-pills{display:inline-flex;background:#111722;border:1px solid #2e3950;border-radius:6px;padding:3px;gap:4px}
.format-pill{border:0;background:transparent;color:#8c9cb8;font-weight:700;font-size:13px;padding:7px 14px;border-radius:4px;cursor:pointer;transition:all .15s ease}
.format-pill:hover{color:#fff}
.format-pill.active{background:#76a7ff;color:#10131a;box-shadow:0 1px 3px rgba(0,0,0,0.3)}

/* Quick Save Section */
.quick-save-section{margin-top:18px;background:#141a25;border:1px solid #2d384d;border-radius:8px;padding:14px 16px}
.quick-save-header{display:flex;align-items:center;justify-content:flex-end;margin-bottom:14px}
.toggle-group{display:inline-flex;background:#0d1117;border:1px solid #2e3950;border-radius:6px;padding:2px}
.toggle-btn{background:transparent;border:0;color:#8c9cb8;font-size:12px;font-weight:600;padding:5px 14px;border-radius:4px;cursor:pointer;transition:all .15s ease}
.toggle-btn:hover{color:#fff}
.toggle-btn.active{background:#273650;color:#76a7ff;box-shadow:0 1px 3px rgba(0,0,0,0.3)}

.folder-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
@media (max-width:640px){.folder-grid{grid-template-columns:repeat(2,1fr)}}
@media (max-width:440px){.folder-grid{grid-template-columns:1fr}}

.folder-chip{position:relative;display:flex;align-items:center;gap:10px;background:#1a2232;border:1px solid #2d3b54;border-radius:8px;padding:10px 12px;cursor:pointer;text-align:left;transition:all .15s ease;user-select:none;color:#eef2ff;font:inherit;box-sizing:border-box;min-height:54px;overflow:hidden}
.folder-chip:hover{background:#232f45;border-color:#76a7ff;transform:translateY(-1px)}
.folder-chip.saving{border-color:#76a7ff;background:#202b3e;pointer-events:none}
.folder-chip.saving .folder-text{padding-right:36px}
.folder-icon{font-size:18px;line-height:1;flex-shrink:0}
.folder-text{min-width:0;flex:1;display:flex;flex-direction:column;gap:2px}
.folder-name{font-weight:600;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.folder-sub{font-size:10px;color:#8c9cb8;line-height:1.25;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;direction:rtl;text-align:left}
.folder-sub bdi{direction:ltr;unicode-bidi:isolate}

/* 9th button: Custom */
.folder-chip.custom-folder-chip{background:#25334c;border-color:#3e537a}
.folder-chip.custom-folder-chip:hover{background:#2f405f;border-color:#9cbaff}

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

/* Media Preview Expansion/Collapse Animation */
.preview-container {
  display: grid;
  grid-template-rows: 0fr;
  opacity: 0;
  margin: 0;
  padding: 0;
  transition: grid-template-rows 0.44s cubic-bezier(0.16, 1, 0.3, 1),
              opacity 0.32s cubic-bezier(0.16, 1, 0.3, 1);
  will-change: grid-template-rows, opacity;
}
.preview-container.expanded {
  grid-template-rows: 1fr;
  opacity: 1;
  transition: grid-template-rows 0.48s cubic-bezier(0.16, 1, 0.3, 1),
              opacity 0.38s cubic-bezier(0.16, 1, 0.3, 1);
}
.preview-content {
  min-height: 0;
  overflow: hidden;
  transform: translateY(-8px);
  transition: transform 0.40s cubic-bezier(0.16, 1, 0.3, 1);
  will-change: transform;
}
.preview-container.expanded .preview-content {
  transform: translateY(0);
  transition: transform 0.48s cubic-bezier(0.16, 1, 0.3, 1);
}
@media (prefers-reduced-motion: reduce) {
  .preview-container, .preview-content {
    transition: none !important;
  }
}
.media-preview{display:block;clear:both}
.media-preview video,.media-preview img{display:block;max-width:100%;max-height:380px;border-radius:8px;margin:14px auto 0;background:#0d121c}
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
</style></head><body><h1>Media Downloader</h1><form id="form" autocomplete="off" onsubmit="event.preventDefault()">
<div class="entry"><input id="url" type="text" placeholder="Paste a YouTube, TikTok, Instagram, X, or Reddit link" required autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" data-lpignore="true"><button id="action" type="button" aria-label="Paste link from clipboard and load preview" title="Paste link from clipboard and load preview"></button></div>

<div id="preview" class="preview-container"><div id="previewContent" class="preview-content"></div></div>

<section class="quick-save-section" id="quickSaveSection">
  <div class="quick-save-header">
    <div class="toggle-group" role="tablist" aria-label="Folder sorting">
      <button type="button" id="toggleFrequent" class="toggle-btn active" title="Show top 8 most frequent save folders">Most Frequent</button>
      <button type="button" id="toggleRecent" class="toggle-btn" title="Show top 8 most recent save folders">Recent</button>
    </div>
  </div>
  <div id="folderList" class="folder-grid"></div>
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

<script>
const input = document.getElementById('url'),
      button = document.getElementById('action'),
      output = document.getElementById('preview'),
      previewContent = document.getElementById('previewContent'),
      dialog = document.getElementById('errorDialog'),
      errorText = document.getElementById('errorText'),
      copyStatus = document.getElementById('copyStatus'),
      folderList = document.getElementById('folderList'),
      toggleFrequent = document.getElementById('toggleFrequent'),
      toggleRecent = document.getElementById('toggleRecent');

let selectedUrl = '',
    previewData = null,
    currentMode = 'video',
    folderSortMode = localStorage.getItem('mdt_folder_sort') || 'frequent',
    foldersData = { frequent: [], recent: [] },
    activeJobUrl = '',
    pollTimer = null,
    activeChipElement = null,
    chipHudTimer = null,
    collapseAnimTimer = null,
    copyStatusTimer = null;

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
  selectedUrl = '';
  previewData = null;
  currentMode = 'video';
  activeJobUrl = '';

  const activeVideo = previewContent ? previewContent.querySelector('video') : null;
  if (activeVideo) {
    try { activeVideo.pause(); } catch (_) {}
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

function renderFolders() {
  const list = (foldersData[folderSortMode] || []).slice(0, 8);
  folderList.innerHTML = '';
  toggleFrequent.classList.toggle('active', folderSortMode === 'frequent');
  toggleRecent.classList.toggle('active', folderSortMode === 'recent');

  list.forEach(folder => {
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

  // 9th slot: Custom
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
  folderList.appendChild(customChip);
}

function setFolderSort(mode) {
  folderSortMode = mode;
  localStorage.setItem('mdt_folder_sort', mode);
  renderFolders();
}
toggleFrequent.onclick = () => setFolderSort('frequent');
toggleRecent.onclick = () => setFolderSort('recent');

async function loadFolders() {
  try {
    const res = await fetch('/api/folders');
    if (res.ok) {
      foldersData = await res.json();
      renderFolders();
    }
  } catch (_) {}
}

function renderPreview(data) {
  clearTimeout(collapseAnimTimer);
  const redditGif = data.fallback === 'reddit_gif';
  const canGif = data.seconds <= 30;

  let formatOptionsHtml = '';
  if (redditGif) {
    currentMode = 'gif';
    formatOptionsHtml = `
      <section class="format-options">
        <span class="format-label">Source Format:</span>
        <div class="format-pills">
          <button type="button" class="format-pill active" data-mode="gif">Reddit GIF</button>
        </div>
      </section>`;
  } else {
    formatOptionsHtml = `
      <section class="format-options">
        <span class="format-label">Select Format:</span>
        <div class="format-pills">
          <button type="button" class="format-pill ${currentMode === 'video' ? 'active' : ''}" data-mode="video">MP4 Video</button>
          ${canGif ? `<button type="button" class="format-pill ${currentMode === 'gif' ? 'active' : ''}" data-mode="gif">GIF Image</button>` : ''}
          <button type="button" class="format-pill ${currentMode === 'audio' ? 'active' : ''}" data-mode="audio">MP3 Audio</button>
        </div>
      </section>`;
  }

  const media = redditGif
    ? (data.thumbnail ? `<img src="${esc(data.thumbnail)}" alt="Reddit GIF preview">` : '')
    : (data.media_url ? `<video controls preload="metadata" src="${esc(data.media_url)}"></video>` : '');

  previewContent.innerHTML = formatOptionsHtml + (media ? `<div class="media-preview">${media}</div>` : '');

  previewContent.querySelectorAll('.format-pill').forEach(pill => {
    pill.onclick = () => {
      previewContent.querySelectorAll('.format-pill').forEach(p => p.classList.remove('active'));
      pill.classList.add('active');
      currentMode = pill.dataset.mode;
    };
  });

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

    def do_GET(self) -> None:
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
            content_length = int(self.headers.get("Content-Length", 0))
            raw_data = self.rfile.read(content_length) if content_length > 0 else b"{}"
            data = json.loads(raw_data.decode("utf-8") if raw_data else "{}")

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
                    subprocess.Popen(["explorer", str(Path(target).resolve())])
                    self.send_json({"ok": True})
                else:
                    self.send_json({"error": "Folder not found"}, 400)
                return

            url = str(data.get("url", "")).strip()
            if not re.match(r"^https?://", url): raise ValueError("Please enter a valid http(s) link.")
            provider = provider_for(url)
            if not provider: raise ValueError("Supported sources: YouTube, TikTok, Instagram, X, and Reddit.")
            if self.path == "/api/preview": self.send_json(preview(url)); return
            if self.path != "/api/download": self.send_json({"error": "Not found"}, 404); return
            mode = str(data.get("mode", "video"))
            if mode not in {"video", "gif", "audio"}: raise ValueError("Unknown download format.")
            direct_url = DIRECT_MEDIA.get(url)
            if direct_url and mode != "gif": raise ValueError("This Reddit post contains a GIF; choose Save GIF.")

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
            if direct_url: job["direct_url"] = direct_url
            with LOCK: JOBS.append(job)
            threading.Thread(target=download, args=(job, provider), daemon=True).start()
            self.send_json({
                "message": f"Started download to {path}.",
                "path": path,
                "folder": str(Path(path).parent),
                "filename": Path(path).name,
                "mode": mode,
            })
        except (ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
            self.send_json({"error": str(exc)}, 400)

    def log_message(self, *_: object) -> None: pass

if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    address = f"http://127.0.0.1:{server.server_port}"
    if sys.stdout is not None:
        try:
            print(f"Open {address}", flush=True)
        except Exception:
            pass
    threading.Timer(.4, lambda: webbrowser.open(address)).start()
    server.serve_forever()
