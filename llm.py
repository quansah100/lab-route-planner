"""The two AI engines the tool can use.

local  (default, free): a model running on your own computer through Ollama
                        (https://ollama.com). Set OLLAMA_MODEL to choose it.
claude (optional, paid): the Claude API. Needs ANTHROPIC_API_KEY.

Both return a validated pydantic object for a given schema.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")


class EngineError(RuntimeError):
    pass


def ask_local(system: str, user: str, schema: type[T], num_ctx: int = 16384) -> T:
    """Ask the local Ollama model; the answer is forced into `schema`."""
    body = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "format": schema.model_json_schema(),
        "options": {"temperature": 0, "num_ctx": num_ctx},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            content = json.load(r)["message"]["content"]
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        if e.code == 404:
            raise EngineError(f"Ollama doesn't have the model '{OLLAMA_MODEL}'. "
                              f"Run: ollama pull {OLLAMA_MODEL}") from None
        raise EngineError(f"Ollama error {e.code}: {detail}") from None
    except urllib.error.URLError:
        raise EngineError(f"Can't reach Ollama at {OLLAMA_URL}. Is the Ollama app running?") from None
    try:
        return schema.model_validate_json(content)
    except ValidationError as e:
        raise EngineError(f"The local model returned an unusable answer: {e.errors()[0]['msg']}") from None


def ask_claude(system: str, user: str, schema: type[T], effort: str = "high") -> T:
    """Ask Claude (paid). Streams, so long answers don't time out."""
    import anthropic

    client = anthropic.Anthropic(max_retries=6)
    with client.beta.messages.stream(
        model=CLAUDE_MODEL,
        max_tokens=32000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # if the request is declined, the API retries on a fallback model
        output_config={"effort": effort},
        system=system,
        messages=[{"role": "user", "content": user}],
        output_format=schema,
    ) as stream:
        response = stream.get_final_message()
    if response.stop_reason == "refusal":
        raise EngineError("Claude declined this request.")
    if response.parsed_output is None:
        raise EngineError(f"Could not read Claude's answer (stop_reason={response.stop_reason}).")
    return response.parsed_output


def ask(engine: str, system: str, user: str, schema: type[T], effort: str = "high") -> T:
    if engine == "local":
        return ask_local(system, user, schema)
    if engine == "claude":
        return ask_claude(system, user, schema, effort)
    raise ValueError(f"unknown engine '{engine}' (use 'local' or 'claude')")


def check_local() -> None:
    """Fail early with a clear message if Ollama or the model is missing."""
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=10) as r:
            names = {m["name"] for m in json.load(r).get("models", [])}
    except urllib.error.URLError:
        raise EngineError(f"Can't reach Ollama at {OLLAMA_URL}. Install it from https://ollama.com "
                          f"and make sure the app is running.") from None
    wanted = OLLAMA_MODEL if ":" in OLLAMA_MODEL else OLLAMA_MODEL + ":latest"
    if wanted not in names:
        raise EngineError(f"The model '{OLLAMA_MODEL}' isn't downloaded yet. Run: ollama pull {OLLAMA_MODEL}")
