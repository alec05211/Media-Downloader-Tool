"""Local, public-media downloader with provider-specific adapter sections."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
import tkinter as tk
from tkinter import filedialog

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

    def command(self, url: str, destination: str, audio_only: bool) -> list[str]:
        # Each provider gets an adapter here. Platform-specific options should only
        # be added when they are officially supported and do not bypass controls.
        # Running as a module guarantees the same Python environment used to
        # start this app is also used to locate the installed yt-dlp package.
        args = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--restrict-filenames", "--newline"]
        if audio_only:
            args += ["-x", "--audio-format", "mp3"]
        else:
            # Prefer a progressive (already muxed) file. This intentionally
            # trades a little maximum resolution for one finished download
            # without requiring FFmpeg to merge separate audio/video streams.
            args += ["-f", "best[ext=mp4]/best"]
        return args + ["-o", str(Path(destination) / "%(extractor)s" / "%(title)s.%(ext)s"), url]


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


def choose_destination() -> str:
    """Show Windows' native folder picker; a cancel retains the default folder."""
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    selected = filedialog.askdirectory(initialdir=DOWNLOADS, title="Choose download folder")
    root.destroy()
    return selected or str(DOWNLOADS)


def run_download(job: dict[str, str], provider: Provider, audio_only: bool) -> None:
    try:
        process = subprocess.Popen(
            provider.command(job["url"], job["destination"], audio_only), cwd=APP_ROOT,
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
<style>body{font-family:system-ui,sans-serif;max-width:780px;margin:48px auto;background:#10131a;color:#eef2ff;padding:0 20px}h1{margin-bottom:4px}.muted{color:#aab4cd}form{background:#1c2230;border:1px solid #2e3950;border-radius:10px;padding:18px;margin:20px 0}input{width:100%;box-sizing:border-box;padding:12px;border-radius:6px;border:1px solid #4b5b7c;background:#111722;color:white;font-size:15px}button,summary{margin-top:12px;padding:10px 14px;background:#76a7ff;border:0;border-radius:6px;font-weight:700;cursor:pointer;display:inline-block}details{display:inline-block;margin-left:8px;position:relative}details>div{position:absolute;right:0;z-index:1;width:230px;background:#273247;border:1px solid #4b5b7c;border-radius:7px;padding:12px;box-shadow:0 8px 24px #0008}details label{margin:0;display:block}details input{width:auto}footer{font-size:13px;color:#aab4cd}</style></head><body>
<h1>Media Downloader</h1><p class="muted">Local downloads for public media you own or are allowed to save.</p>
<form id="form"><label>Video link</label><input id="url" type="url" placeholder="Paste a YouTube, TikTok, Instagram, X, or Reddit link" required><button>Confirm download</button><details><summary>Options</summary><div><label><input id="audio" type="checkbox"> Extract audio as MP3</label></div></details><p id="message" class="muted"></p></form>
<footer>No DRM, login/session bypass, private-content access, or watermark removal is included. Some platforms may serve branded media; this app does not alter it.</footer>
<script>let currentUrl='';async function check(){if(!currentUrl)return;let d=await fetch('/api/status').then(r=>r.json());let j=d.jobs.find(x=>x.url===currentUrl);if(!j)return;if(j.status==='complete'){message.textContent='Download complete.';currentUrl=''}else if(j.status==='failed'){message.textContent='Download failed: '+(j.log||'Unknown error');currentUrl=''}}form.onsubmit=async e=>{e.preventDefault();currentUrl=url.value;message.textContent='Choose a destination folder in the window that opens…';let r=await fetch('/api/download',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:currentUrl,audio:audio.checked})});let d=await r.json();message.textContent=d.message||d.error;if(r.ok)form.reset();else currentUrl=''};setInterval(check,1500)</script></body></html>'''


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
            destination = choose_destination()
            job = {"url": url, "provider": provider.name, "destination": destination, "status": "downloading", "log": ""}
            with LOCK: JOBS.append(job)
            threading.Thread(target=run_download, args=(job, provider, bool(payload.get("audio"))), daemon=True).start()
            self.send_json({"message": f"Started {provider.name} download to {destination}."})
        except (ValueError, json.JSONDecodeError) as exc: self.send_json({"error": str(exc)}, 400)

    def log_message(self, *_: object) -> None: pass


if __name__ == "__main__":
    address = "http://127.0.0.1:8765"
    print(f"Open {address}")
    threading.Timer(0.4, lambda: webbrowser.open(address)).start()
    ThreadingHTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
