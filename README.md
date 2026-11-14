# Media Downloader

A local browser app with customized adapters for YouTube, TikTok, Instagram, X, and Reddit plus a generic public-page adapter for other HTTP(S) links. It uses `yt-dlp` and public page metadata for media that you own or are authorized to save.

## Architecture

Providers implement the same adapter interface and contribute an ordered set of public, no-login resolution strategies. The shared resolution pipeline records each strategy outcome, so adding or replacing a source method does not require changing the web handler. YouTube resolution accepts separate best-video and best-audio streams and merges them into MP4 with FFmpeg, falling back to a combined stream when one is available. The packaged yt-dlp default dependencies include its current external JavaScript challenge scripts, and installed Deno or Node runtimes are detected by yt-dlp. TikTok includes direct web-hydration metadata, public API fallback, and yt-dlp strategies. Reddit includes direct-GIF, modern Shreddit metadata (packaged MP4 and HLS streams), embed metadata, yt-dlp, and legacy JSON/page fallbacks. Unmatched public HTTP(S) links route through the generic adapter, which tries yt-dlp and then discovers `<video>`, `<source>`, Open Graph, JSON-LD, HLS, and DASH candidates. It ranks discovered media using page prominence, resolution, duration, and available content length.

Preview-resolution traces are written locally to `diagnostics/resolution.jsonl`. Confirmed or user-reported method regressions belong in `diagnostics/method-failures.md`; the project-local `.codex/skills/media-downloader-diagnostics/SKILL.md` defines that maintenance workflow.

## Run it

Double-click `application.exe` in the root of this folder. It opens the web application directly in your default browser with no terminal or console window.

The server continues running in the background after the browser tab closes.

For a console-based developer launch with visible terminal output, run:

```powershell
py -m pip install -r requirements.txt
py app.py
```

The app opens a fresh local browser page automatically, using an available port. Paste a link and click **Confirm**. The app presents an MP4 option labeled with its resolution; clips of 30 seconds or less also receive a `.gif format` option. Selecting a format immediately opens Windows' **Save As** dialog, where you choose the exact filename and destination. The app remembers that destination for the next Save As dialog. Cancelling uses a random filename directly in the last selected folder—no source-named folders are created. The included runtime dependency provides GIF conversion automatically.

The downloader and editor share one view. The **Media Downloader** title sits inline with the URL field, paste control, and local-video button. Use that video button to choose a file from your computer, or paste a public video link. A local file automatically expands into editing, while a pasted link begins as a compact preview. The compact preview and editor are one morphing card: its thumbnail grows into the centered video stage, its title moves into the compact toolbar, and the compact row disappears instead of remaining above the editor. The toolbar shows the live crop ratio and a **Preserve source** toggle; hover the toggle for its full description. As soon as pasted-link metadata is ready, the app starts materializing a temporary local MP4 cache in the background. Clicking the video toggles play/pause, while clicking only the empty editor background smoothly returns the card to its compact state. After a successful edited-video save, the complete expanded preview fades and collapses in one motion. Cropping works like a photo editor, and the rendered crop preview keeps the largest available vertical height without stretching.

The editor uses the native Windows file picker so it can safely distinguish a local source file from a cached pasted video. Saving is handled only by the Most Frequent/Recent folder choices and the Custom destination. **Preserve original source video** is off by default. When off, the selected folder receives the edited MP4 and a local original is deleted only after FFmpeg succeeds. When on, the local original remains untouched. Pasted links have no external local original to delete. A successful export closes the editor, and child processes run without console windows.

## Windows application package

Run `powershell -ExecutionPolicy Bypass -File build.ps1` to produce `application.exe` directly in the project root (and in `dist/`). This is a standalone, no-console local application: it bundles its Python runtime and all current dependencies, then opens the browser UI directly. It does not require Python or an internet connection on the machine where it is launched.

After changing application code or `requirements.txt`, rebuild the executable with:

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1
```

This updates `application.exe` in the root and in `dist/`.

For normal Windows installation, compile `installer.iss` using Inno Setup 6 after building. The installer creates a Start-menu entry and offers an optional desktop shortcut.

As a lightweight installer with no additional tooling, run `install.ps1` once. It copies `application.exe` to the local app folder and creates Start-menu and desktop shortcuts.

Both installers register the `media-downloader://` Windows protocol for Windows launchers and start the local server quietly at user sign-in on `http://127.0.0.1:38471/`. Chrome may keep a directly bookmarked custom-protocol URL in a blank tab instead of handing it to Windows, so bookmark the local HTTP address or use the pinned extension button for reliable one-click browser access. When the app is already running, the launcher reuses its local server and opens a fresh tab instead of starting another background server.

## Chrome right-click menu

The unpacked extension in `chrome-extension/` adds **Download with Media Downloader** when you right-click an image, video, audio element, or link. It copies the selected media URL when Chrome permits clipboard access and opens the compatible media or post URL through the background local server in a new tab.

Clicking the extension's toolbar button also opens Media Downloader without prefilling a URL. On a cold start, the extension launches the installed background server and waits for its health check before opening the local page, avoiding a transient connection-refused tab. Pin the extension from Chrome's Extensions menu for one-click access.

To install it locally:

1. Open `chrome://extensions` in Chrome.
2. Enable **Developer mode**.
3. Click **Load unpacked** and select this project's `chrome-extension` folder.
4. Run `install.ps1` or the normal installer once to install and start the background local server.

The extension has the fixed ID `ohajoejdjjakbicandnlljppfpkhimcf`. The downloader keeps customized handling for the named providers and sends other public page or media links through the generic adapter. On sites such as Instagram and Reddit, the extension sends the containing post URL when the exact CDN media URL is not a usable provider link.

## Boundaries

The tool does not use credentials, work around DRM, download private/restricted content, generate proof-of-origin tokens, or remove watermarks. It requests the best anonymous source media exposed by the supported extractor; YouTube may still require sign-in, proof-of-origin attestation, or DRM for particular videos, clients, or networks, and those cases remain intentionally unsupported. Platforms can change availability or branding at any time. Respect each platform's terms and the creator's rights.
