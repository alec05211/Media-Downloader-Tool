"""Local downloader for public media the user is permitted to save."""
from __future__ import annotations
import json, re, secrets, shutil, string, subprocess, sys, threading, webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import Tk, filedialog
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).parent
DOWNLOADS = ROOT / "downloads"; DOWNLOADS.mkdir(exist_ok=True)
LAST_SAVE_FOLDER = ROOT / ".last_save_folder"
PREVIEWS: dict[str, dict[str, object]] = {}

@dataclass(frozen=True)
class Provider:
    name: str
    domains: tuple[str, ...]
    def matches(self, host: str) -> bool:
        return any(host == d or host.endswith("." + d) for d in self.domains)
    def command(self, url: str, path: str, mode: str, ffmpeg: str | None = None) -> list[str]:
        # A progressive MP4 is one combined audio/video file.
        output = str(Path(path).with_suffix("")) + ".%(ext)s"
        args = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--newline", "-f", "best[ext=mp4]", "-o", output]
        if ffmpeg: args += ["--ffmpeg-location", ffmpeg]
        return args + [url]

PROVIDERS = (Provider("YouTube",("youtube.com","youtu.be")), Provider("TikTok",("tiktok.com",)),
             Provider("Instagram",("instagram.com",)), Provider("X",("x.com","twitter.com")), Provider("Reddit",("reddit.com","redd.it")))
JOBS: list[dict[str,str]] = []; LOCK = threading.Lock()

def provider_for(url: str) -> Provider | None:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return next((p for p in PROVIDERS if p.matches(host)), None)

def safe_name(value: str) -> str:
    return (re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .") or "download")[:120]

def choose_path(title: str, mode: str) -> str:
    suffix = ".gif" if mode == "gif" else ".mp4"
    # A neutral default prevents source titles from becoming part of the path.
    name = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12)) + suffix
    try:
        last_folder = Path(LAST_SAVE_FOLDER.read_text(encoding="utf-8").strip())
        if not last_folder.is_dir(): last_folder = DOWNLOADS
    except OSError: last_folder = DOWNLOADS
    root = Tk(); root.withdraw(); root.attributes("-topmost", True)
    result = filedialog.asksaveasfilename(title="Save download as", initialdir=last_folder, initialfile=name,
        defaultextension=suffix, filetypes=[("GIF image","*.gif")] if mode == "gif" else [("MP4 video","*.mp4")])
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

def classify_failure(text: str) -> tuple[str, str]:
    value = text.lower()
    if "no module named 'yt_dlp'" in value: return "DEP001", "The yt-dlp engine is unavailable to this Python installation. Run start.bat to install dependencies."
    if "encoder not found" in value or "conversion failed" in value: return "GIF002", "FFmpeg could not encode the downloaded video as GIF. The source may use an unsupported stream or the conversion process was interrupted."
    if "ffmpeg" in value: return "GIF001", "GIF conversion could not start because the FFmpeg runtime is missing or failed to initialize. Restart with start.bat; if it persists, reinstall dependencies."
    if "private video" in value or "login" in value or "sign in" in value: return "AUTH001", "The source requires sign-in, is private, or restricts anonymous downloads. This app does not use account credentials."
    if "age" in value and "restrict" in value: return "AUTH002", "The source is age-restricted and requires an authenticated session."
    if "geo" in value or "not available in your country" in value: return "SRC001", "The source has a geographic availability restriction."
    if "requested format is not available" in value or "no video formats" in value: return "FMT001", "No single progressive MP4 stream was exposed by the source. The app intentionally avoids separate audio/video files."
    if "unable to download" in value or "timed out" in value or "connection" in value: return "NET001", "The source could not be reached or stopped responding. Check your connection and retry."
    if "permission denied" in value or "access is denied" in value: return "FS001", "Windows denied permission to write to the selected destination. Choose another folder or adjust its permissions."
    if "video unavailable" in value or "this video is not available" in value or "does not exist" in value: return "SRC002", "The source video is unavailable, removed, or the link is no longer valid."
    return "DL999", "The downloader returned an unclassified error. Hover for the original diagnostic output."

def preview(url: str) -> dict[str, object]:
    command = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-warnings", "-J", "-f", "best[ext=mp4]", url]
    run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
    if run.returncode: raise ValueError(run.stderr.strip() or "This link could not be processed.")
    info = json.loads(run.stdout); w,h = info.get("width"),info.get("height")
    token = secrets.token_urlsafe(18)
    PREVIEWS[token] = {"url": str(info.get("url") or ""), "headers": info.get("http_headers") or {}}
    return {"title":str(info.get("title") or "download"),"thumbnail":str(info.get("thumbnail") or ""),
            "media_url":f"/api/stream/{token}" if info.get("url") else "","duration":str(info.get("duration_string") or ""),
            "seconds":float(info.get("duration") or 0),"resolution":f"{w}×{h}" if w and h else "Best available resolution"}

def download(job: dict[str,str], provider: Provider) -> None:
    try:
        ffmpeg = ffmpeg_binary()
        if job["mode"] == "gif" and not ffmpeg: raise RuntimeError("GIF conversion runtime is missing. Run start.bat again, then retry.")
        process = subprocess.Popen(provider.command(job["url"],job["path"],job["mode"],ffmpeg), cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        output,_ = process.communicate()
        if process.returncode == 0 and job["mode"] == "gif":
            intermediate = str(Path(job["path"]).with_suffix(".mp4"))
            conversion = subprocess.run(
                [ffmpeg, "-y", "-i", intermediate, "-vf", "fps=12,scale=640:-1:flags=lanczos", job["path"]],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            output += conversion.stdout + conversion.stderr
            if conversion.returncode == 0:
                Path(intermediate).unlink(missing_ok=True)
            else:
                process.returncode = conversion.returncode
        job["log"] = output[-4000:]; job["status"] = "complete" if process.returncode == 0 else "failed"
        if job["status"] == "failed": job["code"], job["error"] = classify_failure(output)
    except Exception as exc:
        job["status"] = "failed"; job["log"] = str(exc); job["code"], job["error"] = classify_failure(str(exc))

PAGE = r'''<!doctype html><html><head><meta charset="utf-8"><title>Media Downloader</title><style>body{font-family:system-ui,sans-serif;max-width:780px;margin:48px auto;background:#10131a;color:#eef2ff;padding:0 20px}.muted{color:#aab4cd}form{background:#1c2230;border:1px solid #2e3950;border-radius:10px;padding:18px;margin:20px 0}.entry{display:flex}.entry input{min-width:0;flex:1;box-sizing:border-box;padding:12px;border-radius:6px 0 0 6px;border:1px solid #4b5b7c;background:#111722;color:white;font-size:15px}.entry button{margin:0;border-radius:0 6px 6px 0;padding:10px 14px;background:#76a7ff;border:0;font-weight:700;cursor:pointer}.choice{margin:12px 8px 0 0;padding:10px 14px;background:#273650;color:white;border:0;border-radius:6px;font-weight:700;cursor:pointer}video,#preview img{display:block;max-width:100%;max-height:380px;border-radius:8px;margin-top:14px}footer{font-size:13px;color:#aab4cd}</style></head><body><h1>Media Downloader</h1><p class="muted">Local downloads for public media you own or are allowed to save.</p><form id="form"><label>Video link</label><div class="entry"><input id="url" type="url" placeholder="Paste a YouTube, TikTok, Instagram, X, or Reddit link" required><button id="action">Paste link</button></div><p id="message" class="muted"></p><div id="preview"></div></form><footer>No DRM, login/session bypass, private-content access, or watermark removal is included.</footer><script>let active='';const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));function updateAction(){action.textContent=url.value.trim()?'Confirm':'Paste link'}async function choose(){message.textContent='Checking available formats…';preview.innerHTML='';let r=await fetch('/api/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:url.value})}),d=await r.json();if(!r.ok){message.textContent=d.error;return}let gif=d.seconds>0&&d.seconds<=30;preview.innerHTML=`<p><b>${esc(d.title)}</b> ${d.duration?'('+esc(d.duration)+')':''}</p><p>Choose a format to download:</p><button class="choice" data-mode="video">MP4 video — ${esc(d.resolution)}</button>`+(gif?'<button class="choice" data-mode="gif">.gif format</button>':'')+(d.media_url?`<video controls preload="metadata" poster="${esc(d.thumbnail)}"><source src="${esc(d.media_url)}"></video>`:(d.thumbnail?`<img src="${esc(d.thumbnail)}" alt="Video thumbnail">`:''));document.querySelectorAll('.choice').forEach(b=>b.onclick=()=>start(b.dataset.mode,d.title));message.textContent=gif?'Select MP4 or GIF to download.':'Select MP4 to download. GIF is available for clips up to 30 seconds.'}async function start(mode,title){active=url.value;message.textContent='Choose an exact file name and save location…';let r=await fetch('/api/download',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:active,mode,title})}),d=await r.json();message.textContent=d.message||d.error}async function status(){if(!active)return;let d=await fetch('/api/status').then(r=>r.json()),j=d.jobs.find(x=>x.url===active);if(!j)return;if(j.status==='complete'){message.textContent='Download complete.';active=''}if(j.status==='failed'){message.textContent='Download failed: '+j.log;active=''}}form.onsubmit=async e=>{e.preventDefault();if(url.value.trim())choose();else{try{url.value=(await navigator.clipboard.readText()).trim();updateAction();if(url.value)message.textContent='Link pasted. Click Confirm.'}catch{message.textContent='Clipboard access was not allowed. Paste a link into the field.'}}};url.oninput=()=>{preview.innerHTML='';updateAction()};setInterval(status,1500)</script></body></html>'''

# Keep the surface intentionally minimal: input/action, choices, and preview.
PAGE = PAGE.replace('<p class="muted">Local downloads for public media you own or are allowed to save.</p>', '')
PAGE = PAGE.replace('<label>Video link</label>', '')
PAGE = PAGE.replace("<p><b>${esc(d.title)}</b> ${d.duration?'('+esc(d.duration)+')':''}</p>", '')
PAGE = PAGE.replace('<p id="message" class="muted"></p>', '<span id="message" hidden></span>')
PAGE = PAGE.replace('<footer>No DRM, login/session bypass, private-content access, or watermark removal is included.</footer>', '')
PAGE = PAGE.replace('</style>', '.entry button:disabled{background:#526174;cursor:wait}.spinner{display:inline-block;width:13px;height:13px;border:2px solid #cbd5e1;border-right-color:transparent;border-radius:50%;animation:spin .7s linear infinite;vertical-align:middle}@keyframes spin{to{transform:rotate(360deg)}}</style>')
PAGE = PAGE.replace('</style>', 'dialog{max-width:680px;width:calc(100% - 40px);background:#1c2230;color:#eef2ff;border:1px solid #4b5b7c;border-radius:10px;padding:20px}dialog::backdrop{background:#0009}dialog textarea{width:100%;height:210px;box-sizing:border-box;background:#111722;color:#eef2ff;border:1px solid #4b5b7c;border-radius:6px;padding:10px;resize:vertical}dialog button{margin:12px 8px 0 0;padding:8px 12px;border:0;border-radius:6px;background:#76a7ff;font-weight:700;cursor:pointer}</style>')
PAGE = PAGE.replace('<div id="preview"></div>', '<div id="preview"></div><dialog id="errorDialog"><textarea id="errorText" readonly aria-label="Error details"></textarea><button type="button" onclick="copyError()">Copy details</button><button type="button" onclick="errorDialog.close()">Close</button></dialog>')
PAGE = PAGE.replace("let active='';", "let active='';function showError(code,text,raw=''){errorText.value=code+' — '+text+(raw?'\\n\\nDiagnostic: '+raw:'');errorDialog.showModal();errorText.focus();errorText.select()}async function copyError(){try{await navigator.clipboard.writeText(errorText.value)}catch{errorText.select();document.execCommand('copy')}}function clearError(){}")
PAGE = PAGE.replace("function clearError(){}", "function clearError(){}function setLoading(loading){action.disabled=loading;action.innerHTML=loading?'<span class=\"spinner\"></span>':(url.value.trim()?'Confirm':'Paste link')}")
PAGE = PAGE.replace("if(!r.ok){message.textContent=d.error;return}", "if(!r.ok){showError(d.code||'API400',d.error);return}")
PAGE = PAGE.replace("message.textContent=d.message||d.error}", "if(!r.ok)showError(d.code||'API400',d.error)}")
PAGE = PAGE.replace("if(j.status==='complete'){message.textContent='Download complete.';active=''}if(j.status==='failed'){message.textContent='Download failed: '+j.log;active=''}", "if(j.status==='complete'){active=''}if(j.status==='failed'){showError(j.code||'DL999',j.error||'Download failed.',j.log);active=''}")
PAGE = PAGE.replace("url.oninput=()=>{preview.innerHTML='';updateAction()}", "url.oninput=()=>{preview.innerHTML='';clearError();updateAction()}")
PAGE = PAGE.replace("async function choose(){message.textContent='Checking available formats…';", "async function choose(){setLoading(true);message.textContent='Checking available formats…';")
PAGE = PAGE.replace("if(!r.ok){showError(d.code||'API400',d.error);return}", "if(!r.ok){showError(d.code||'API400',d.error);setLoading(false);return}")
PAGE = PAGE.replace("GIF is available for clips up to 30 seconds.'}", "GIF is available for clips up to 30 seconds.';setLoading(false)}")

class Handler(BaseHTTPRequestHandler):
    def send_json(self, value: object, status: int=200) -> None:
        body=json.dumps(value).encode(); self.send_response(status); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self) -> None:
        if self.path=="/api/status":
            with LOCK: self.send_json({"jobs":list(reversed(JOBS))})
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
            if mode not in {"video","gif"}: raise ValueError("Unknown download format.")
            path=choose_path(str(data.get("title","download")),mode)
            job={"url":url,"mode":mode,"path":path,"status":"downloading","log":""}
            with LOCK: JOBS.append(job)
            threading.Thread(target=download,args=(job,provider),daemon=True).start()
            self.send_json({"message":f"Started download to {path}."})
        except (ValueError,json.JSONDecodeError,subprocess.TimeoutExpired) as exc: self.send_json({"error":str(exc)},400)
    def log_message(self,*_:object)->None: pass

if __name__=="__main__":
    address="http://127.0.0.1:8765"; print(f"Open {address}"); threading.Timer(.4,lambda:webbrowser.open(address)).start(); ThreadingHTTPServer(("127.0.0.1",8765),Handler).serve_forever()
