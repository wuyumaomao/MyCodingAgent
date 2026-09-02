from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI


def main() -> int:
    load_dotenv(Path.cwd() / ".env")
    api_key = os.getenv("CODING_AGENT_API_KEY")
    model = os.getenv("CODING_AGENT_MODEL")
    base_url = os.getenv("CODING_AGENT_BASE_URL")

    missing = [
        name
        for name, value in (
            ("CODING_AGENT_API_KEY", api_key),
            ("CODING_AGENT_MODEL", model),
            ("CODING_AGENT_BASE_URL", base_url),
        )
        if not value
    ]
    if missing:
        print(f"Missing configuration: {', '.join(missing)}", file=sys.stderr)
        return 2

    print(f"Testing model={model} base_url={base_url}")
    try:
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=30.0)
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "你好，请只回复：连通成功"}],
        )
        if not hasattr(response, "choices"):
            print("HTTP request succeeded, but the response is not OpenAI-compatible")
            print(f"Response type: {type(response).__name__}")
            print(f"Response preview: {str(response)[:500]}", file=sys.stderr)
            return 3
        print("API request succeeded")
        print(f"Response: {response.choices[0].message.content}")
        return 0
    except Exception as exc:
        print(f"API request failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
