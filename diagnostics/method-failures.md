# Provider method failures

## 2026-09-13 — Reddit — `reddit-json`, `old-reddit-json`, and `reddit-page-metadata`

- Status: tentative; reported and reproduced for post `1vp3vdf` from this local environment.
- Observed failure: Reddit returned `403 Blocked` from the standard public JSON endpoint; the legacy endpoint and public post page returned an anti-bot challenge instead of post metadata. The `preview.redd.it` GIF request also returned `403` to non-browser clients.
- Impact: the public GIF fallback cannot resolve that post, even though the post is viewable in a browser.
- Current handling: the ordered Reddit adapter records each attempted method and returns a clear no-login failure when none exposes a GIF. Revisit if Reddit restores a documented public metadata route or its public CDN behavior changes.
