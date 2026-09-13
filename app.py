"""Local downloader for public media the user is permitted to save."""
from __future__ import annotations
import json, re, secrets, shutil, string, subprocess, sys, threading, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import Tk, filedialog
from urllib.request import Request, urlopen

from media_downloader.diagnostics import record_download_failure, record_resolution
from media_downloader.pipeline import ProviderAdapter, resolve
from media_downloader.providers import RedditProvider, YtDlpProvider

ROOT = Path(__file__).parent
DOWNLOADS = ROOT / "downloads"; DOWNLOADS.mkdir(exist_ok=True)
LAST_SAVE_FOLDER = ROOT / ".last_save_folder"
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

def choose_path(title: str, mode: str) -> str:
    suffix = {"gif": ".gif", "audio": ".mp3"}.get(mode, ".mp4")
    # A neutral default prevents source titles from becoming part of the path.
    name = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12)) + suffix
    try:
        last_folder = Path(LAST_SAVE_FOLDER.read_text(encoding="utf-8").strip())
        if not last_folder.is_dir(): last_folder = DOWNLOADS
    except OSError: last_folder = DOWNLOADS
    root = Tk(); root.withdraw(); root.attributes("-topmost", True)
    result = filedialog.asksaveasfilename(title="Save download as", initialdir=last_folder, initialfile=name,
        defaultextension=suffix, filetypes=([("GIF image","*.gif")] if mode == "gif" else [("MP3 audio","*.mp3")] if mode == "audio" else [("MP4 video","*.mp4")]))
    root.destroy()
    chosen = Path(result) if result else last_folder / name
    try: LAST_SAVE_FOLDER.write_text(str(chosen.parent), encoding="utf-8")
    except OSError: pass
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
        output_template = str(Path(job["path"]).with_suffix("")) + ".%(ext)s"
        command = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--newline", "-f", "best[ext=mp4]", "-o", output_template]
        if ffmpeg: command += ["--ffmpeg-location", ffmpeg]
        process = subprocess.Popen(command + [job["url"]], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        output,_ = process.communicate()
        if process.returncode == 0 and job["mode"] in {"gif", "audio"}:
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
                process.returncode = conversion.returncode
        job["log"] = output[-4000:]; job["status"] = "complete" if process.returncode == 0 else "failed"
        if job["status"] == "failed":
            record_download_failure(ROOT, job["url"], provider.name, job.get("strategy", "download"), output)
            job["code"], job["error"] = classify_failure(output)
    except Exception as exc:
        record_download_failure(ROOT, job["url"], provider.name, job.get("strategy", "download"), str(exc))
        job["status"] = "failed"; job["log"] = str(exc); job["code"], job["error"] = classify_failure(str(exc))

PAGE = r'''<!doctype html><html><head><meta charset="utf-8"><title>Media Downloader</title><style>
body{font-family:system-ui,sans-serif;max-width:780px;margin:48px auto;background:#10131a;color:#eef2ff;padding:0 20px}form{background:#1c2230;border:1px solid #2e3950;border-radius:10px;padding:18px;margin:20px 0}.entry{display:flex}.entry input{min-width:0;flex:1;box-sizing:border-box;padding:12px;border-radius:6px 0 0 6px;border:1px solid #4b5b7c;background:#111722;color:#fff;font-size:15px}.entry button,.choice{border:0;font-weight:700;cursor:pointer}.entry button{margin:0;border-radius:0 6px 6px 0;padding:10px 14px;background:#76a7ff}.entry button:disabled{background:#526174;cursor:wait}.choice{padding:10px 14px;background:#273650;color:#fff;border-radius:6px}.format-options{display:flex;justify-content:center;align-items:center;flex-wrap:wrap;gap:8px;margin:18px auto}.media-preview{display:block;clear:both}.media-preview video,.media-preview img{display:block;max-width:100%;max-height:380px;border-radius:8px;margin:14px auto 0}.spinner{display:inline-block;width:13px;height:13px;border:2px solid #cbd5e1;border-right-color:transparent;border-radius:50%;animation:spin .7s linear infinite;vertical-align:middle}@keyframes spin{to{transform:rotate(360deg)}}dialog{position:relative;max-width:680px;width:calc(100% - 40px);background:#1c2230;color:#eef2ff;border:1px solid #4b5b7c;border-radius:10px;padding:20px}dialog::backdrop{background:#0009}.dialog-controls{position:absolute;top:10px;right:10px;display:flex;align-items:center;gap:8px}.icon-button{display:grid;place-items:center;width:36px;height:36px;padding:0;border:0;border-radius:6px;background:#273650;color:#eef2ff;cursor:pointer}.close-button{background:transparent;font-size:24px;line-height:1}#errorText{margin:8px 160px 0 0;white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;color:inherit}.copy-status{color:#aab4cd;font-size:13px}
</style></head><body><h1>Media Downloader</h1><form id="form"><div class="entry"><input id="url" type="url" placeholder="Paste a YouTube, TikTok, Instagram, X, or Reddit link" required><button id="action" type="button">Paste link</button></div><div id="preview"></div><dialog id="errorDialog" aria-labelledby="errorText"><div class="dialog-controls"><span class="copy-status" id="copyStatus" role="status" hidden>Copied</span><button class="icon-button" id="copyError" type="button" aria-label="Copy error details" title="Copy error details"><svg aria-hidden="true" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="11" height="11" rx="1"></rect><path d="M15 9V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v9a1 1 0 0 0 1 1h4"></path></svg></button><button class="icon-button close-button" id="closeError" type="button" aria-label="Close error dialog">×</button></div><pre id="errorText"></pre></dialog></form><script>
const input=document.getElementById('url'),button=document.getElementById('action'),output=document.getElementById('preview'),dialog=document.getElementById('errorDialog'),errorText=document.getElementById('errorText'),copyStatus=document.getElementById('copyStatus');let selectedUrl='',copyStatusTimer;
const esc=value=>String(value).replace(/[&<>"]/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[char]));
function update(){button.disabled=false;button.textContent=input.value.trim()?'Confirm':'Paste link'}
function showError(code,text,raw=''){errorText.textContent=code+' — '+text+(raw?'\n\nDiagnostic: '+raw:'');dialog.showModal()}
function setBusy(){button.disabled=true;button.innerHTML='<span class="spinner"></span>'}
async function copyError(){let copied=false;try{await navigator.clipboard.writeText(errorText.textContent);copied=true}catch{const selection=getSelection(),range=document.createRange();range.selectNodeContents(errorText);selection.removeAllRanges();selection.addRange(range);copied=document.execCommand('copy');selection.removeAllRanges()}if(copied){copyStatus.hidden=false;clearTimeout(copyStatusTimer);copyStatusTimer=setTimeout(()=>copyStatus.hidden=true,900)}}
async function startDownload(mode,title){setBusy();try{const response=await fetch('/api/download',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:selectedUrl,mode,title})}),data=await response.json();if(!response.ok)showError(data.code||'API400',data.error||'Download could not start.')}catch{showError('NET001','The downloader could not be reached.')}finally{update()}}
async function chooseFormats(){const candidate=input.value.trim();if(!candidate)return;setBusy();output.innerHTML='';try{const response=await fetch('/api/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:candidate})}),data=await response.json();if(!response.ok){showError(data.code||'API400',data.error||'Could not inspect this link.');return}selectedUrl=candidate;input.value='';update();const redditGif=data.fallback==='reddit_gif',gif=data.seconds<=30;const options=redditGif?'<section class="format-options"><span>Reddit GIF found:</span><button type="button" class="choice" data-mode="gif">Save GIF</button></section>':'<section class="format-options"><span>Choose a format to download:</span><button type="button" class="choice" data-mode="video">MP4</button>'+(gif?'<button type="button" class="choice" data-mode="gif">GIF</button>':'')+'<button type="button" class="choice" data-mode="audio">MP3</button></section>';const media=redditGif?(data.thumbnail?'<img src="'+esc(data.thumbnail)+'" alt="Reddit GIF preview">':''):(data.media_url?'<video controls preload="metadata" src="'+esc(data.media_url)+'"></video>':'');output.innerHTML=options+(media?'<div class="media-preview">'+media+'</div>':'');output.querySelectorAll('.choice').forEach(choice=>choice.onclick=()=>startDownload(choice.dataset.mode,data.title))}catch{showError('NET001','The downloader could not be reached.')}finally{update()}}
button.onclick=async()=>{if(input.value.trim()){await chooseFormats();return}setBusy();try{const data=await fetch('/api/clipboard').then(response=>response.json());input.value=(data.text||'').trim();if(!input.value)showError('CLIP001','The clipboard is empty or could not be read.')}catch{showError('CLIP001','The Windows clipboard could not be read.')}finally{update()}};input.addEventListener('input',()=>{selectedUrl='';output.innerHTML='';update()});document.getElementById('closeError').onclick=()=>dialog.close();document.getElementById('copyError').onclick=copyError;update();
</script></body></html>'''

class Handler(BaseHTTPRequestHandler):
    def send_json(self, value: object, status: int=200) -> None:
        body=json.dumps(value).encode(); self.send_response(status); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self) -> None:
        if self.path=="/api/status":
            with LOCK: self.send_json({"jobs":list(reversed(JOBS))})
            return
        if self.path=="/api/clipboard":
            self.send_json({"text": clipboard_text()})
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
        self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.end_headers(); self.wfile.write(PAGE.encode())
    def do_POST(self) -> None:
        try:
            data=json.loads(self.rfile.read(int(self.headers.get("Content-Length",0)))); url=str(data.get("url","")).strip()
            if not re.match(r"^https?://",url): raise ValueError("Please enter a valid http(s) link.")
            provider=provider_for(url)
            if not provider: raise ValueError("Supported sources: YouTube, TikTok, Instagram, X, and Reddit.")
            if self.path=="/api/preview": self.send_json(preview(url)); return
            if self.path!="/api/download": self.send_json({"error":"Not found"},404); return
            mode=str(data.get("mode","video"))
            if mode not in {"video","gif","audio"}: raise ValueError("Unknown download format.")
            direct_url = DIRECT_MEDIA.get(url)
            if direct_url and mode != "gif": raise ValueError("This Reddit post contains a GIF; choose Save GIF.")
            path=choose_path(str(data.get("title","download")),mode)
            job={"url":url,"mode":mode,"path":path,"status":"downloading","log":""}
            job["strategy"] = RESOLUTION_STRATEGIES.get(url, "download")
            if direct_url: job["direct_url"] = direct_url
            with LOCK: JOBS.append(job)
            threading.Thread(target=download,args=(job,provider),daemon=True).start()
            self.send_json({"message":f"Started download to {path}."})
        except (ValueError,json.JSONDecodeError,subprocess.TimeoutExpired) as exc: self.send_json({"error":str(exc)},400)
    def log_message(self,*_:object)->None: pass

if __name__=="__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    address = f"http://127.0.0.1:{server.server_port}"
    print(f"Open {address}")
    threading.Timer(.4, lambda: webbrowser.open(address)).start()
    server.serve_forever()
