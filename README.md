# Media Downloader

A local browser app with separate adapter entries for YouTube, TikTok, Instagram, X, and Reddit. It uses `yt-dlp` for public media that you own or are authorized to save.

## Architecture

Providers implement the same adapter interface and contribute an ordered set of public, no-login resolution strategies. The shared resolution pipeline records each strategy outcome, so adding or replacing a source method does not require changing the web handler. Reddit includes direct-GIF, yt-dlp, public JSON, legacy JSON, and public-page metadata strategies; the first successful strategy supplies the media candidate.

Preview-resolution traces are written locally to `diagnostics/resolution.jsonl`. Confirmed or user-reported method regressions belong in `diagnostics/method-failures.md`; the project-local `.codex/skills/media-downloader-diagnostics/SKILL.md` defines that maintenance workflow.

## Run it

Double-click `start.bat`, or run:

```powershell
py -m pip install -r requirements.txt
py app.py
```

The app opens a fresh local browser page automatically, using an available port. Paste a link and click **Confirm**. The app presents an MP4 option labeled with its resolution; clips of 30 seconds or less also receive a `.gif format` option. Selecting a format immediately opens Windows' **Save As** dialog, where you choose the exact filename and destination. The app remembers that destination for the next Save As dialog. Cancelling uses a random filename directly in the last selected folder—no source-named folders are created. The included runtime dependency provides GIF conversion automatically.

## Boundaries

The tool does not use credentials, work around DRM, download private/restricted content, or remove watermarks. It requests the best source media exposed by the supported extractor; platforms can change availability or branding at any time. Respect each platform's terms and the creator's rights.
