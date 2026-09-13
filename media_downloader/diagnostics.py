from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .models import ResolutionResult


def record_resolution(root: Path, url: str, result: ResolutionResult) -> None:
    """Persist a compact local trace; failure promotion is governed by the project skill."""
    folder = root / "diagnostics"
    folder.mkdir(exist_ok=True)
    event = {
        "at": datetime.now(UTC).isoformat(),
        "url": url,
        "provider": result.provider,
        "resolved": result.candidate is not None,
        "attempts": [attempt.__dict__ for attempt in result.attempts],
    }
    with (folder / "resolution.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps(event, ensure_ascii=False) + "\n")


def record_download_failure(root: Path, url: str, provider: str, strategy: str, detail: str) -> None:
    folder = root / "diagnostics"
    folder.mkdir(exist_ok=True)
    event = {"at": datetime.now(UTC).isoformat(), "url": url, "provider": provider,
             "strategy": strategy, "download_failed": True, "detail": detail[-2000:]}
    with (folder / "resolution.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps(event, ensure_ascii=False) + "\n")
