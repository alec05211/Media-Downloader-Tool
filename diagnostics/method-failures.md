# Provider method failures

## 2026-09-13 — Reddit — `reddit-json`, `old-reddit-json`, and `reddit-page-metadata`

- Status: resolved; post `1vp3vdf` and modern Reddit posts resolve via the `reddit-shreddit` strategy.
- Observed failure: Reddit returned `403 Blocked` from the standard public JSON endpoint; the legacy endpoint and public post page returned an anti-bot challenge instead of post metadata. The `preview.redd.it` GIF request also returned `403` to non-browser clients.
- Impact: the public GIF fallback could not resolve that post using legacy JSON endpoints.
- Current handling: `RedditShredditStrategy` inspects modern `<shreddit-post>` and `<shreddit-player>` elements with desktop browser headers, extracting packaged MP4 permutations, direct `i.redd.it` GIFs, and HLS playlists. Native FFmpeg muxing supports progressive MP4, animated GIF, and MP3 conversion.
