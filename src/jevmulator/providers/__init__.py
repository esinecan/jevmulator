"""Upstream providers."""

from .base import Provider, ProviderResult, UpstreamCall
from .fake import FakeProvider
from .openai_compatible import OpenAICompatibleProvider

__all__ = [
    "Provider",
    "ProviderResult",
    "UpstreamCall",
    "FakeProvider",
    "OpenAICompatibleProvider",
    "build_provider",
]


def build_provider(config) -> Provider:
    """Construct the provider named by ``config.provider``."""
    if config.provider == "fake":
        return FakeProvider(config)
    return OpenAICompatibleProvider(config)
