# Media Downloader

A local browser app with separate adapter entries for YouTube, TikTok, Instagram, X, and Reddit. It uses `yt-dlp` for public media that you own or are authorized to save.

## Run it

Double-click `start.bat`, or run:

```powershell
py -m pip install -r requirements.txt
py app.py
```

Open `http://127.0.0.1:8765` if it does not open automatically. When you confirm a download, Windows will ask where to save it. Cancel that picker to use the app's `downloads/` folder. Normal video downloads use a single, already-combined media stream; this avoids separate audio and video files. The expandable Options control holds MP3 extraction and can accommodate future settings. Files are organized by extractor.

## Boundaries

The tool does not use credentials, work around DRM, download private/restricted content, or remove watermarks. It requests the best source media exposed by the supported extractor; platforms can change availability or branding at any time. Respect each platform's terms and the creator's rights.
