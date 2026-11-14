# Provider method failures

## 2026-11-06 — YouTube — `yt-dlp` single progressive MP4 selection

- Status: confirmed and replaced for anonymous public videos; provider-enforced authentication, proof-of-origin attestation, and DRM remain unsupported.
- Observed failure: preview and download forced `best[ext=mp4]`. A public video could expose separate MP4 video and M4A audio streams while exposing no combined progressive MP4, causing yt-dlp to return `Requested format is not available` even though downloadable public formats existed.
- Impact: affected YouTube links failed during preview, so no download could start. The same restrictive selector was also used by the download pass.
- Current handling: the shared yt-dlp configuration now prefers `bv*[ext=mp4]+ba[ext=m4a]`, falls back to combined MP4 and then yt-dlp's best other public formats, merges/remuxes the result to MP4 with FFmpeg, enables installed Deno or Node challenge runtimes, and packages yt-dlp's matching EJS scripts. No credentials, proxies, PO-token providers, or access-control bypasses are introduced.

## 2026-09-13 — Reddit — `reddit-json`, `old-reddit-json`, and `reddit-page-metadata`

- Status: resolved; post `1vp3vdf` and modern Reddit posts resolve via the `reddit-shreddit` strategy.
- Observed failure: Reddit returned `403 Blocked` from the standard public JSON endpoint; the legacy endpoint and public post page returned an anti-bot challenge instead of post metadata. The `preview.redd.it` GIF request also returned `403` to non-browser clients.
- Impact: the public GIF fallback could not resolve that post using legacy JSON endpoints.
- Current handling: `RedditShredditStrategy` inspects modern `<shreddit-post>` and `<shreddit-player>` elements with desktop browser headers, extracting packaged MP4 permutations, direct `i.redd.it` GIFs, and HLS playlists. Native FFmpeg muxing supports progressive MP4, animated GIF, and MP3 conversion.
