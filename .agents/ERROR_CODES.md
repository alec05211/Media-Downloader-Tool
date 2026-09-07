# Media Downloader Error Codes

The red indicator beside the Paste/Confirm control appears only after a failure. Hover it to see the code, explanation, and the downloader's original diagnostic text.

| Code | Meaning | Resolution |
| --- | --- | --- |
| API400 | The local app rejected the submitted URL or request. | Use a complete public HTTPS link from a supported source. |
| DEP001 | yt-dlp is unavailable to the Python runtime running the app. | Close the app and run start.bat to install dependencies. |
| GIF001 | GIF conversion could not initialize FFmpeg. | Run start.bat again. If it repeats, reinstall the requirements. |
| GIF002 | FFmpeg could not encode the downloaded MP4 as a GIF. | Retry once; if it repeats, use the MP4 choice or report the alert diagnostic. |
| AUTH001 | The source needs sign-in, is private, or forbids anonymous access. | Use a public video. The app deliberately does not use account credentials. |
| AUTH002 | The source is age-gated and requires an authenticated session. | Use a non-restricted public source; no authentication bypass is provided. |
| SRC001 | The source is unavailable in the current geographic region. | Try media that is available in your location. |
| SRC002 | The video was removed, is unavailable, or the link is invalid/expired. | Open the original link in a browser, copy its current public URL, then retry. |
| FMT001 | The source did not expose a combined MP4 stream. | This app does not download separate audio and video streams; choose another source/video. |
| NET001 | The source could not be reached, timed out, or terminated the connection. | Check your connection, wait briefly, and retry. |
| FS001 | Windows refused to write to the selected folder. | Choose a writable folder or change its permissions. |
| DL999 | An unclassified downloader failure occurred. | Hover the indicator for the full raw diagnostic. Record that text when reporting the issue. |

## Error reporting requirements

Every failed download must carry both a stable code and a short plain-language explanation. The raw diagnostic is retained as hover text for DL999 and other classified failures. New failure categories should receive a new stable code instead of overloading an existing one.
