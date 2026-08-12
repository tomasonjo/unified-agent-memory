#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["litellm>=1.0", "tenacity>=8.0"]
# ///
"""LLM access for Unified Agent Memory's background agents.

The interactive session already has a model; this module is not for it.
It serves the plugin's background jobs, the ones hooks kick off around
the session: memory extraction at stop, consolidation of what
accumulated, and similar work that needs a completion of its own
without touching the conversation.

One entry point, :func:`llm_complete`, behind one backend knob
(``UAM_LLM_BACKEND``):

- ``claude-cli`` (default): spawns headless Claude Code (``claude -p``),
  which authenticates exactly like the interactive session that
  triggered the hook, so the subscription login already on the machine
  pays for the call and there is nothing to set up. Runs Haiku by
  default (``UAM_CLAUDE_CLI_MODEL`` overrides) so background calls stay
  quick and cheap. The spawned session carries the
  ``UAM_IN_LLM_SUBPROCESS`` sentinel so this plugin's own hooks no-op
  inside it instead of capturing the nested session or recursing.
- ``litellm``: the escape hatch to every other provider.
  ``UAM_LLM_MODEL`` is a LiteLLM model string, authenticated by the
  matching provider key (``OPENAI_API_KEY`` and so on), which LiteLLM
  reads from the environment itself. Anthropic model strings can still
  run on the subscription: the helper reads a fresh Claude Code OAuth
  token from the platform credential store (Keychain on macOS,
  libsecret on Linux, ``CLAUDE_CODE_OAUTH_TOKEN`` as a headless
  fallback) and passes it as the call-scoped ``api_key``, falling back
  to ``ANTHROPIC_API_KEY``, and degrading to headless ``claude -p``
  when neither is usable.

The module mirrors the sister meta-knowledge-graph project's design and
imports with the standard library alone; litellm is imported lazily
inside the backend that needs it. Runnable directly for a smoke test:

    uv run --script hooks/llm.py "Reply with one word: ready"
"""

from __future__ import annotations

import base64
import binascii
import getpass
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
if str(HOOK_DIR) not in sys.path:
    sys.path.insert(0, str(HOOK_DIR))

from common import LLM_SUBPROCESS_SENTINEL  # noqa: E402

DEFAULT_LLM_MODEL = "anthropic/claude-haiku-4-5"
DEFAULT_CLAUDE_CLI_MODEL = "haiku"
DEFAULT_CLAUDE_CLI_TIMEOUT = 300.0
DEFAULT_NUM_RETRIES = 2

CLAUDE_CODE_CREDENTIAL_SERVICE = "Claude Code-credentials"
OAUTH_TOKEN_PREFIX = "sk-ant-oat"
OAUTH_READ_TIMEOUT_SECONDS = 5
OAUTH_EXPIRY_GRACE_SECONDS = 60


def llm_backend() -> str:
    """``claude-cli`` (default) or ``litellm``; anything unrecognised
    means claude-cli, the backend that needs no configuration."""
    explicit = (os.getenv("UAM_LLM_BACKEND") or "").strip().lower()
    if explicit == "litellm":
        return "litellm"
    return "claude-cli"


def llm_model() -> str:
    return os.getenv("UAM_LLM_MODEL") or DEFAULT_LLM_MODEL


def num_retries() -> int:
    try:
        return max(0, int(os.getenv("UAM_LLM_NUM_RETRIES", DEFAULT_NUM_RETRIES)))
    except ValueError:
        return DEFAULT_NUM_RETRIES


def llm_complete(messages: list[dict[str, str]], *, model: str | None = None) -> str:
    """Single entry point for hook LLM calls.

    Takes chat-style messages, dispatches to the configured backend, and
    returns the response text; callers keep their own post-processing.
    """
    if llm_backend() == "claude-cli":
        return _complete_claude_cli(messages)
    return _complete_litellm(messages, model)


# --- litellm backend ---------------------------------------------------------


def _complete_litellm(messages: list[dict[str, str]], model: str | None) -> str:
    model_name = model or llm_model()
    api_key = _anthropic_subscription_token(model_name)

    # An Anthropic model with no readable subscription token and no
    # explicit key would only fail inside litellm. The claude-cli
    # backend still works there, because the CLI refreshes its own
    # OAuth token, so degrade to it instead of erroring; as a side
    # effect the refreshed token makes the next litellm call direct.
    if (
        _is_anthropic_model(model_name)
        and not api_key
        and not _has_explicit_anthropic_auth()
        and shutil.which("claude")
    ):
        print(
            "[llm] no usable subscription token or Anthropic key; "
            "falling back to headless claude",
            file=sys.stderr,
        )
        return _complete_claude_cli(messages)

    # litellm auto-loads a .env on first import in its default DEV mode,
    # which would reintroduce exactly the project-file leak the canonical
    # env file exists to close; pin PRODUCTION so the environment the
    # hooks loaded is the one the call sees.
    os.environ.setdefault("LITELLM_MODE", "PRODUCTION")
    import litellm

    kwargs: dict = {
        "model": model_name,
        "messages": messages,
        "num_retries": num_retries(),
    }
    if api_key:
        kwargs["api_key"] = api_key
    response = litellm.completion(**kwargs)
    return response.choices[0].message.content or ""


def _has_explicit_anthropic_auth() -> bool:
    return any(
        os.environ.get(key)
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    )


def _is_anthropic_model(model: str) -> bool:
    normalized = model.strip().lower()
    return normalized.startswith(("anthropic/", "anthropic_text/", "claude-"))


def _anthropic_subscription_token(model: str) -> str | None:
    """Call-scoped ``api_key`` override, when the subscription should pay.

    Anthropic models prefer the logged-in Claude Code subscription, so a
    fresh OAuth token is read at call time. Returning ``None`` lets
    LiteLLM fall back to the provider key in the environment, which is
    also the whole story for non-Anthropic models.
    """
    if not _is_anthropic_model(model):
        return None
    return _read_claude_oauth_token()


def _read_claude_oauth_token() -> str | None:
    for raw in _oauth_payload_candidates():
        token = _parse_oauth_payload(raw)
        if token:
            return token
    return None


def _oauth_payload_candidates() -> Iterator[str]:
    """Raw credential payloads, most authoritative first.

    The platform credential store wins because Claude Code refreshes the
    token in place there; the environment variable remains a fallback
    for headless machines with no keychain or libsecret entry.
    """
    if sys.platform == "darwin":
        account = getpass.getuser()
        for cmd in (
            ["security", "find-generic-password",
             "-s", CLAUDE_CODE_CREDENTIAL_SERVICE, "-a", account, "-w"],
            ["security", "find-generic-password",
             "-s", CLAUDE_CODE_CREDENTIAL_SERVICE, "-w"],
        ):
            out = _run_quiet(cmd)
            if out:
                yield out
    elif sys.platform.startswith("linux") and shutil.which("secret-tool"):
        out = _run_quiet(
            ["secret-tool", "lookup",
             "service", CLAUDE_CODE_CREDENTIAL_SERVICE,
             "account", getpass.getuser()]
        )
        if out:
            yield out
    env_token = os.getenv("CLAUDE_CODE_OAUTH_TOKEN")
    if env_token:
        yield env_token


def _run_quiet(cmd: list[str]) -> str | None:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=OAUTH_READ_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = proc.stdout.strip()
    return out if proc.returncode == 0 and out else None


def _parse_oauth_payload(raw: str) -> str | None:
    """Extract a usable access token from one credential payload.

    Accepts both shapes the sources produce: the credential store's JSON
    envelope with ``claudeAiOauth.accessToken``, and a bare token string.
    Expired tokens are rejected here so the caller simply tries the next
    source.
    """
    value = raw.strip()
    if not value:
        return None
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        token = value.removeprefix("Bearer ").strip()
        if not _looks_like_oauth_token(token):
            return None
        return None if _is_expired(_jwt_exp_seconds(token)) else token
    oauth = payload.get("claudeAiOauth") if isinstance(payload, dict) else None
    if not isinstance(oauth, dict):
        return None
    token = oauth.get("accessToken")
    if not isinstance(token, str) or not _looks_like_oauth_token(token):
        return None
    expires_at = _expires_at_seconds(oauth.get("expiresAt"))
    if expires_at is None:
        expires_at = _jwt_exp_seconds(token)
    return None if _is_expired(expires_at) else token


def _looks_like_oauth_token(token: str) -> bool:
    return token.startswith(OAUTH_TOKEN_PREFIX) or len(token.split(".")) == 3


def _expires_at_seconds(value) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    # The credential JSON stores milliseconds; JWT exp stores seconds.
    return float(value) / 1000 if value > 10_000_000_000 else float(value)


def _jwt_exp_seconds(token: str) -> float | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        return None
    exp = data.get("exp") if isinstance(data, dict) else None
    return float(exp) if isinstance(exp, (int, float)) else None


def _is_expired(expires_at: float | None) -> bool:
    return expires_at is not None and expires_at + OAUTH_EXPIRY_GRACE_SECONDS < time.time()


# --- claude-cli backend ------------------------------------------------------


def _claude_cli_env() -> dict[str, str]:
    """Environment for the spawned ``claude -p``.

    Sets the recursion sentinel so this plugin's own hooks no-op in the
    nested session, and clears competing Anthropic credentials so the
    subscription login wins the auth precedence chain;
    ``ANTHROPIC_API_KEY`` would otherwise take priority in ``-p`` mode.
    ``CLAUDE_CODE_OAUTH_TOKEN`` is kept.
    """
    env = os.environ.copy()
    env[LLM_SUBPROCESS_SENTINEL] = "1"
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        env.pop(key, None)
    return env


def _complete_claude_cli_once(messages: list[dict[str, str]]) -> str:
    """One completion through the harness's headless agent.

    System messages replace the default Claude Code prompt (the coding
    agent persona is not wanted here); the rest is piped via stdin,
    which is robust for large inputs where argument length is capped.
    Returns the ``result`` text from the ``--output-format json``
    envelope. Deliberately not run with ``--bare``: bare mode never
    reads OAuth or the keychain, which would defeat subscription auth.
    """
    system = "\n\n".join(
        m["content"]
        for m in messages
        if m.get("role") == "system" and m.get("content")
    )
    user = "\n\n".join(
        m["content"]
        for m in messages
        if m.get("role") != "system" and m.get("content")
    )
    cmd = ["claude", "-p", "--output-format", "json"]
    if system:
        cmd += ["--system-prompt", system]
    # Haiku by default: a hook's background completion should stay quick
    # and cheap on the subscription; UAM_CLAUDE_CLI_MODEL raises it.
    model = (os.getenv("UAM_CLAUDE_CLI_MODEL") or DEFAULT_CLAUDE_CLI_MODEL).strip()
    if model:
        cmd += ["--model", model]
    timeout = float(
        os.getenv("UAM_CLAUDE_CLI_TIMEOUT") or DEFAULT_CLAUDE_CLI_TIMEOUT
    )
    proc = subprocess.run(
        cmd,
        input=user,
        capture_output=True,
        text=True,
        env=_claude_cli_env(),
        cwd=tempfile.gettempdir(),
        timeout=timeout,
    )
    if proc.returncode != 0:
        # Auth and API failures arrive as a JSON envelope on stdout with
        # an empty stderr, so mine the envelope for the actual reason.
        detail = (proc.stderr or "").strip()
        if not detail:
            try:
                detail = str(json.loads(proc.stdout).get("result") or "").strip()
            except (json.JSONDecodeError, AttributeError):
                detail = (proc.stdout or "").strip()
        raise RuntimeError(f"claude -p exited {proc.returncode}: {detail[:300]}")
    envelope = json.loads(proc.stdout)
    return envelope.get("result") or ""


def _complete_claude_cli(messages: list[dict[str, str]]) -> str:
    """:func:`_complete_claude_cli_once` with retries and backoff."""
    retries = num_retries()
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return _complete_claude_cli_once(messages)
        except (
            RuntimeError,
            OSError,
            subprocess.TimeoutExpired,
            json.JSONDecodeError,
        ) as exc:
            last_exc = exc
            if attempt == retries:
                break
            time.sleep(2**attempt)
    assert last_exc is not None
    raise last_exc


if __name__ == "__main__":
    from common import load_env

    load_env()
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Reply with one word: ready"
    print(llm_complete([{"role": "user", "content": prompt}]))
