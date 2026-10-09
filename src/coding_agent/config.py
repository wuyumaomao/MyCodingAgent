from __future__ import annotations

from dataclasses import dataclass
import os
from argparse import Namespace
from pathlib import Path

from dotenv import dotenv_values


DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_TIMEOUT = 600.0
SUPPORTED_PROVIDERS = frozenset({"openai", "deepseek"})


class ConfigError(ValueError):
    """Raised when required model configuration is missing."""


@dataclass(frozen=True)
class Settings:
    api_key: str
    model: str
    provider: str = "openai"
    base_url: str = DEFAULT_BASE_URL
    timeout: float = DEFAULT_TIMEOUT
    context_window_tokens: int | None = None

    @classmethod
    def from_args_and_env(cls, args: Namespace) -> "Settings":
        file_values = {
            str(key): str(value)
            for key, value in dotenv_values(dotenv_path=Path.cwd() / ".env").items()
            if value is not None
        }

        def environment(name: str) -> str | None:
            return os.getenv(name)

        def file_value(name: str) -> str | None:
            return file_values.get(name)

        provider = (
            getattr(args, "provider", None)
            or environment("CODING_AGENT_PROVIDER")
            or file_value("CODING_AGENT_PROVIDER")
            or "openai"
        ).lower()
        if provider not in SUPPORTED_PROVIDERS:
            raise ConfigError(f"Unsupported provider: {provider}")
        prefix = provider.upper()
        api_key = (
            getattr(args, "api_key", None)
            or environment(f"{prefix}_API_KEY")
            or environment("CODING_AGENT_API_KEY")
            or file_value(f"{prefix}_API_KEY")
            or file_value("CODING_AGENT_API_KEY")
        )
        model = (
            getattr(args, "model", None)
            or environment(f"{prefix}_MODEL")
            or environment("CODING_AGENT_MODEL")
            or file_value(f"{prefix}_MODEL")
            or file_value("CODING_AGENT_MODEL")
        )
        base_url = (
            getattr(args, "base_url", None)
            or environment(f"{prefix}_BASE_URL")
            or environment("CODING_AGENT_BASE_URL")
            or file_value(f"{prefix}_BASE_URL")
            or file_value("CODING_AGENT_BASE_URL")
            or DEFAULT_BASE_URL
        )
        arg_timeout = getattr(args, "timeout", None)
        timeout_value = (
            arg_timeout
            if arg_timeout is not None
            else environment(f"{prefix}_TIMEOUT")
            or environment("CODING_AGENT_TIMEOUT")
            or file_value(f"{prefix}_TIMEOUT")
            or file_value("CODING_AGENT_TIMEOUT")
        )
        if timeout_value is None:
            timeout = DEFAULT_TIMEOUT
        else:
            try:
                timeout = float(timeout_value)
            except (TypeError, ValueError) as exc:
                raise ConfigError("Timeout must be a positive number") from exc
            if timeout <= 0:
                raise ConfigError("Timeout must be a positive number")
        arg_context_window = getattr(args, "context_window_tokens", None)
        context_value = (
            arg_context_window
            if arg_context_window is not None
            else environment(f"{prefix}_CONTEXT_WINDOW_TOKENS")
            or environment("CODING_AGENT_CONTEXT_WINDOW_TOKENS")
            or file_value(f"{prefix}_CONTEXT_WINDOW_TOKENS")
            or file_value("CODING_AGENT_CONTEXT_WINDOW_TOKENS")
        )
        if context_value is None or context_value == "":
            context_window_tokens = None
        else:
            try:
                context_window_tokens = int(context_value)
            except (TypeError, ValueError) as exc:
                raise ConfigError("Context window tokens must be a positive integer") from exc
            if context_window_tokens <= 0:
                raise ConfigError("Context window tokens must be a positive integer")
        if not api_key:
            raise ConfigError("API key is required (set CODING_AGENT_API_KEY)")
        if not model:
            raise ConfigError("Model is required (set CODING_AGENT_MODEL)")
        return cls(
            provider=provider,
            api_key=api_key,
            model=model,
            base_url=base_url,
            timeout=timeout,
            context_window_tokens=context_window_tokens,
        )
