---
name: media-downloader-diagnostics
description: Maintain resolution diagnostics and documented provider-method regressions for the Media Downloader Tool.
---

# Media Downloader Diagnostics

Use this skill when modifying a provider adapter, resolution strategy, downloader transport, or investigating a reported source failure in this project.

The application writes one local resolution trace per preview request to `diagnostics/resolution.jsonl`. Treat a single trace as evidence for investigation, not a permanent product rule.

When a method is reproducibly broken, a user reports it as failed, or a provider/platform change makes the existing implementation invalid, add a concise entry to `diagnostics/method-failures.md`. Include the date, provider, strategy name, observed failure, scope, and whether the result is confirmed or tentative. Describe what the implementation did and why it no longer succeeds; do not record account credentials, cookies, private URLs, or sensitive diagnostics.

Do not remove a historical entry merely because a retry succeeds. Add a follow-up stating the changed evidence or replacement strategy. Keep public/no-login boundaries intact: diagnostic work must not introduce credential, proxy, anti-bot, DRM, or private-content bypasses.
