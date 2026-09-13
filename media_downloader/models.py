from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MediaCandidate:
    title: str
    kind: str
    source_url: str
    thumbnail_url: str = ""
    duration_seconds: float = 0
    duration_label: str = ""
    resolution: str = "Best available resolution"
    headers: dict[str, str] = field(default_factory=dict)
    direct: bool = False


@dataclass(frozen=True)
class ResolutionAttempt:
    strategy: str
    succeeded: bool
    detail: str


@dataclass(frozen=True)
class ResolutionResult:
    provider: str
    candidate: MediaCandidate | None
    attempts: tuple[ResolutionAttempt, ...]


class ResolutionError(Exception):
    """An expected failure from one public resolution strategy."""
