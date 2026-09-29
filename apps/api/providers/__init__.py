"""Composition root: register additional adapters here without changing callers."""
import os
from .base import ProviderRegistry
from .codex import CodexProvider


def build_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(CodexProvider(
        binary=os.environ.get('CODEX_BINARY', 'codex'),
        model=os.environ.get('CODEX_MODEL') or None,
    ))
    return registry
