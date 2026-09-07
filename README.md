# Media Downloader

A local browser app with separate adapter entries for YouTube, TikTok, Instagram, X, and Reddit. It uses `yt-dlp` for public media that you own or are authorized to save.

## Run it

Double-click `start.bat`, or run:

```powershell
py -m pip install -r requirements.txt
py app.py
```

Open `http://127.0.0.1:8765` if it does not open automatically. Paste a link and click **Confirm**. The app presents an MP4 option labeled with its resolution; clips of 30 seconds or less also receive a `.gif format` option. Selecting a format immediately opens Windows' **Save As** dialog, where you choose the exact filename and destination. Cancelling uses the suggested filename directly in the app's `downloads/` folder—no source-named folders are created. GIF conversion requires [FFmpeg](https://ffmpeg.org/download.html).

## Boundaries

The tool does not use credentials, work around DRM, download private/restricted content, or remove watermarks. It requests the best source media exposed by the supported extractor; platforms can change availability or branding at any time. Respect each platform's terms and the creator's rights.
