"""Local downloader for public media the user is permitted to save."""
from __future__ import annotations
import json, re, shutil, subprocess, sys, threading, webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import Tk, filedialog
from urllib.parse import urlparse

ROOT = Path(__file__).parent
DOWNLOADS = ROOT / "downloads"; DOWNLOADS.mkdir(exist_ok=True)

@dataclass(frozen=True)
class Provider:
    name: str
    domains: tuple[str, ...]
    def matches(self, host: str) -> bool:
        return any(host == d or host.endswith("." + d) for d in self.domains)
    def command(self, url: str, path: str, mode: str) -> list[str]:
        # A progressive MP4 is one combined audio/video file.
        output = str(Path(path).with_suffix("")) + ".%(ext)s"
        args = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--newline", "-f", "best[ext=mp4]", "-o", output]
        if mode == "gif": args += ["--recode-video", "gif"]
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
    name = safe_name(title) + suffix
    root = Tk(); root.withdraw(); root.attributes("-topmost", True)
    result = filedialog.asksaveasfilename(title="Save download as", initialdir=DOWNLOADS, initialfile=name,
        defaultextension=suffix, filetypes=[("GIF image","*.gif")] if mode == "gif" else [("MP4 video","*.mp4")])
    root.destroy()
    return result or str(DOWNLOADS / name)

def preview(url: str) -> dict[str, object]:
    command = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-warnings", "-J", "-f", "best[ext=mp4]", url]
    run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
    if run.returncode: raise ValueError(run.stderr.strip() or "This link could not be processed.")
    info = json.loads(run.stdout); w,h = info.get("width"),info.get("height")
    return {"title":str(info.get("title") or "download"),"thumbnail":str(info.get("thumbnail") or ""),
            "media_url":str(info.get("url") or ""),"duration":str(info.get("duration_string") or ""),
            "seconds":float(info.get("duration") or 0),"resolution":f"{w}×{h}" if w and h else "Best available resolution"}

def download(job: dict[str,str], provider: Provider) -> None:
    try:
        if job["mode"] == "gif" and not shutil.which("ffmpeg"): raise RuntimeError("GIF conversion needs FFmpeg. Install it, restart the app, then try again.")
        process = subprocess.Popen(provider.command(job["url"],job["path"],job["mode"]), cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        output,_ = process.communicate(); job["log"] = output[-4000:]; job["status"] = "complete" if process.returncode == 0 else "failed"
    except Exception as exc: job["status"] = "failed"; job["log"] = str(exc)

PAGE = r'''<!doctype html><html><head><meta charset="utf-8"><title>Media Downloader</title><style>body{font-family:system-ui,sans-serif;max-width:780px;margin:48px auto;background:#10131a;color:#eef2ff;padding:0 20px}.muted{color:#aab4cd}form{background:#1c2230;border:1px solid #2e3950;border-radius:10px;padding:18px;margin:20px 0}input{width:100%;box-sizing:border-box;padding:12px;border-radius:6px;border:1px solid #4b5b7c;background:#111722;color:white;font-size:15px}button{margin:12px 8px 0 0;padding:10px 14px;background:#76a7ff;border:0;border-radius:6px;font-weight:700;cursor:pointer}.choice{background:#273650}video,#preview img{display:block;max-width:100%;max-height:380px;border-radius:8px;margin-top:14px}footer{font-size:13px;color:#aab4cd}</style></head><body><h1>Media Downloader</h1><p class="muted">Local downloads for public media you own or are allowed to save.</p><form id="form"><label>Video link</label><input id="url" type="url" placeholder="Paste a YouTube, TikTok, Instagram, X, or Reddit link" required><button>Confirm</button><p id="message" class="muted"></p><div id="preview"></div></form><footer>No DRM, login/session bypass, private-content access, or watermark removal is included.</footer><script>let active='';const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));async function choose(){message.textContent='Checking available formats…';preview.innerHTML='';let r=await fetch('/api/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:url.value})}),d=await r.json();if(!r.ok){message.textContent=d.error;return}let gif=d.seconds>0&&d.seconds<=30;preview.innerHTML=`<p><b>${esc(d.title)}</b> ${d.duration?'('+esc(d.duration)+')':''}</p><p>Choose a format to download:</p><button class="choice" data-mode="video">MP4 video — ${esc(d.resolution)}</button>`+(gif?'<button class="choice" data-mode="gif">.gif format</button>':'')+(d.media_url?`<video controls preload="metadata" poster="${esc(d.thumbnail)}"><source src="${esc(d.media_url)}"></video>`:(d.thumbnail?`<img src="${esc(d.thumbnail)}" alt="Video thumbnail">`:''));document.querySelectorAll('.choice').forEach(b=>b.onclick=()=>start(b.dataset.mode,d.title));message.textContent=gif?'Select MP4 or GIF to download.':'Select MP4 to download. GIF is available for clips up to 30 seconds.'}async function start(mode,title){active=url.value;message.textContent='Choose an exact file name and save location…';let r=await fetch('/api/download',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:active,mode,title})}),d=await r.json();message.textContent=d.message||d.error}async function status(){if(!active)return;let d=await fetch('/api/status').then(r=>r.json()),j=d.jobs.find(x=>x.url===active);if(!j)return;if(j.status==='complete'){message.textContent='Download complete.';active=''}if(j.status==='failed'){message.textContent='Download failed: '+j.log;active=''}}form.onsubmit=e=>{e.preventDefault();choose()};url.oninput=()=>preview.innerHTML='';setInterval(status,1500)</script></body></html>'''

class Handler(BaseHTTPRequestHandler):
    def send_json(self, value: object, status: int=200) -> None:
        body=json.dumps(value).encode(); self.send_response(status); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self) -> None:
        if self.path=="/api/status":
            with LOCK: self.send_json({"jobs":list(reversed(JOBS))})
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
