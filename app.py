"""Local, public-media downloader with provider-specific adapter sections."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

APP_ROOT = Path(__file__).parent
DOWNLOADS = APP_ROOT / "downloads"
DOWNLOADS.mkdir(exist_ok=True)


@dataclass(frozen=True)
class Provider:
    name: str
    domains: tuple[str, ...]
    description: str

    def matches(self, host: str) -> bool:
        return any(host == domain or host.endswith("." + domain) for domain in self.domains)

    def command(self, url: str, audio_only: bool) -> list[str]:
        # Each provider gets an adapter here. Platform-specific options should only
        # be added when they are officially supported and do not bypass controls.
        executable = shutil.which("yt-dlp") or "yt-dlp"
        args = [executable, "--no-playlist", "--restrict-filenames", "--newline"]
        if audio_only:
            args += ["-x", "--audio-format", "mp3"]
        else:
            args += ["-f", "bv*+ba/b", "--merge-output-format", "mp4"]
        return args + ["-o", str(DOWNLOADS / "%(extractor)s" / "%(title)s.%(ext)s"), url]


PROVIDERS = (
    Provider("YouTube", ("youtube.com", "youtu.be"), "Public videos you are permitted to save."),
    Provider("TikTok", ("tiktok.com",), "Public videos where the platform provides the original."),
    Provider("Instagram", ("instagram.com",), "Public posts you own or have permission to download."),
    Provider("X", ("x.com", "twitter.com"), "Public posts you are permitted to save."),
    Provider("Reddit", ("reddit.com", "redd.it"), "Public posts you are permitted to save."),
)

JOBS: list[dict[str, str]] = []
LOCK = threading.Lock()


def provider_for(url: str) -> Provider | None:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return next((provider for provider in PROVIDERS if provider.matches(host)), None)


def run_download(job: dict[str, str], provider: Provider, audio_only: bool) -> None:
    try:
        process = subprocess.Popen(
            provider.command(job["url"], audio_only), cwd=APP_ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        )
        output, _ = process.communicate()
        job["log"] = output[-5000:]
        job["status"] = "complete" if process.returncode == 0 else "failed"
    except FileNotFoundError:
        job["status"] = "failed"
        job["log"] = "yt-dlp is not installed. Run: py -m pip install -r requirements.txt"
    except Exception as exc:
        job["status"] = "failed"
        job["log"] = str(exc)


PAGE = r'''<!doctype html><html><head><meta charset="utf-8"><title>Media Downloader</title>
<style>body{font-family:system-ui,sans-serif;max-width:780px;margin:48px auto;background:#10131a;color:#eef2ff;padding:0 20px}h1{margin-bottom:4px}.muted{color:#aab4cd}form,.job{background:#1c2230;border:1px solid #2e3950;border-radius:10px;padding:18px;margin:20px 0}input{width:100%;box-sizing:border-box;padding:12px;border-radius:6px;border:1px solid #4b5b7c;background:#111722;color:white;font-size:15px}button{margin-top:12px;padding:10px 14px;background:#76a7ff;border:0;border-radius:6px;font-weight:700;cursor:pointer}label{display:block;margin-top:12px}pre{white-space:pre-wrap;max-height:160px;overflow:auto;color:#c8d4ec}.tag{background:#273650;padding:4px 8px;border-radius:12px;font-size:12px}footer{font-size:13px;color:#aab4cd}</style></head><body>
<h1>Media Downloader</h1><p class="muted">Local downloads for public media you own or are allowed to save.</p>
<form id="form"><label>Video link</label><input id="url" type="url" placeholder="Paste a YouTube, TikTok, Instagram, X, or Reddit link" required><label><input id="audio" type="checkbox" style="width:auto"> Extract audio as MP3</label><button>Download</button><p id="message" class="muted"></p></form>
<h2>Provider adapters</h2><div id="providers" class="muted"></div><h2>Jobs</h2><div id="jobs"></div>
<footer>No DRM, login/session bypass, private-content access, or watermark removal is included. Some platforms may serve branded media; this app does not alter it.</footer>
<script>async function refresh(){let d=await fetch('/api/status').then(r=>r.json());providers.innerHTML=d.providers.map(p=>`<p><span class="tag">${p.name}</span> ${p.description}</p>`).join('');jobs.innerHTML=d.jobs.length?d.jobs.map(j=>`<div class="job"><b>${j.provider}</b> — ${j.status}<br><span class="muted">${j.url}</span>${j.log?`<pre>${j.log}</pre>`:''}</div>`).join(''):'<p class="muted">No downloads yet.</p>'}form.onsubmit=async e=>{e.preventDefault();message.textContent='Starting…';let r=await fetch('/api/download',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:url.value,audio:audio.checked})});let d=await r.json();message.textContent=d.message||d.error; if(r.ok)form.reset();refresh()};refresh();setInterval(refresh,2000)</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def send_json(self, value: object, status: int = 200) -> None:
        body = json.dumps(value).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/api/status":
            with LOCK: self.send_json({"providers": [p.__dict__ for p in PROVIDERS], "jobs": list(reversed(JOBS))})
            return
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(PAGE.encode())

    def do_POST(self) -> None:
        if self.path != "/api/download": self.send_json({"error": "Not found"}, 404); return
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            url = str(payload.get("url", "")).strip()
            if not re.match(r"^https?://", url): raise ValueError("Please enter a valid http(s) link.")
            provider = provider_for(url)
            if not provider: raise ValueError("Supported sources: YouTube, TikTok, Instagram, X, and Reddit.")
            job = {"url": url, "provider": provider.name, "status": "downloading", "log": ""}
            with LOCK: JOBS.append(job)
            threading.Thread(target=run_download, args=(job, provider, bool(payload.get("audio"))), daemon=True).start()
            self.send_json({"message": f"Started {provider.name} download."})
        except (ValueError, json.JSONDecodeError) as exc: self.send_json({"error": str(exc)}, 400)

    def log_message(self, *_: object) -> None: pass


if __name__ == "__main__":
    address = "http://127.0.0.1:8765"
    print(f"Open {address}")
    threading.Timer(0.4, lambda: webbrowser.open(address)).start()
    ThreadingHTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
