from __future__ import annotations

from dataclasses import dataclass
import os
from argparse import Namespace


DEFAULT_BASE_URL = "https://api.openai.com/v1"


class ConfigError(ValueError):
    """Raised when required model configuration is missing."""


@dataclass(frozen=True)
class Settings:
    api_key: str
    model: str
    base_url: str = DEFAULT_BASE_URL

    @classmethod
    def from_args_and_env(cls, args: Namespace) -> "Settings":
        api_key = getattr(args, "api_key", None) or os.getenv("CODING_AGENT_API_KEY")
        model = getattr(args, "model", None) or os.getenv("CODING_AGENT_MODEL")
        base_url = (
            getattr(args, "base_url", None)
            or os.getenv("CODING_AGENT_BASE_URL")
            or DEFAULT_BASE_URL
        )
        if not api_key:
            raise ConfigError("API key is required (set CODING_AGENT_API_KEY)")
        if not model:
            raise ConfigError("Model is required (set CODING_AGENT_MODEL)")
        return cls(api_key=api_key, model=model, base_url=base_url)
