from __future__ import annotations

from dataclasses import dataclass
import os
from argparse import Namespace
from pathlib import Path

from dotenv import load_dotenv


DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_TIMEOUT = 600.0


class ConfigError(ValueError):
    """Raised when required model configuration is missing."""


@dataclass(frozen=True)
class Settings:
    api_key: str
    model: str
    base_url: str = DEFAULT_BASE_URL
    timeout: float = DEFAULT_TIMEOUT

    @classmethod
    def from_args_and_env(cls, args: Namespace) -> "Settings":
        load_dotenv(dotenv_path=Path.cwd() / ".env")
        api_key = getattr(args, "api_key", None) or os.getenv("CODING_AGENT_API_KEY")
        model = getattr(args, "model", None) or os.getenv("CODING_AGENT_MODEL")
        base_url = (
            getattr(args, "base_url", None)
            or os.getenv("CODING_AGENT_BASE_URL")
            or DEFAULT_BASE_URL
        )
        arg_timeout = getattr(args, "timeout", None)
        timeout_value = arg_timeout if arg_timeout is not None else os.getenv("CODING_AGENT_TIMEOUT")
        if timeout_value is None:
            timeout = DEFAULT_TIMEOUT
        else:
            try:
                timeout = float(timeout_value)
            except (TypeError, ValueError) as exc:
                raise ConfigError("Timeout must be a positive number") from exc
            if timeout <= 0:
                raise ConfigError("Timeout must be a positive number")
        if not api_key:
            raise ConfigError("API key is required (set CODING_AGENT_API_KEY)")
        if not model:
            raise ConfigError("Model is required (set CODING_AGENT_MODEL)")
        return cls(api_key=api_key, model=model, base_url=base_url, timeout=timeout)
