from __future__ import annotations

from typing import Protocol

from .models import MediaCandidate, ResolutionAttempt, ResolutionError, ResolutionResult


class ResolutionStrategy(Protocol):
    name: str

    def resolve(self, url: str) -> MediaCandidate:
        """Return a candidate or raise ResolutionError for an expected miss."""


class ProviderAdapter(Protocol):
    name: str

    def matches(self, url: str) -> bool:
        """Whether this adapter owns the supplied URL."""

    def strategies(self, url: str) -> tuple[ResolutionStrategy, ...]:
        """Return public, no-login strategies in their preferred order."""


def resolve(adapter: ProviderAdapter, url: str) -> ResolutionResult:
    attempts: list[ResolutionAttempt] = []
    for strategy in adapter.strategies(url):
        try:
            candidate = strategy.resolve(url)
        except ResolutionError as exc:
            attempts.append(ResolutionAttempt(strategy.name, False, str(exc)))
        except Exception as exc:
            attempts.append(ResolutionAttempt(strategy.name, False, f"Unexpected {type(exc).__name__}: {exc}"))
        else:
            attempts.append(ResolutionAttempt(strategy.name, True, "Media resolved."))
            return ResolutionResult(adapter.name, candidate, tuple(attempts))
    return ResolutionResult(adapter.name, None, tuple(attempts))
