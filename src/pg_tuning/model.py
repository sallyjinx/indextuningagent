"""A minimal, one-request OpenAI-compatible client. No SDK retries or tools."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit

import httpx


@dataclass(frozen=True)
class ModelConfig:
    base_url: str
    model: str
    api_key: str = ""
    timeout_seconds: float = 60.0
    connect_timeout_seconds: float = 10.0
    min_request_interval_seconds: float = 0.0
    temperature: float = 0.0
    max_completion_tokens: int = 400
    token_limit_parameter: str = "max_completion_tokens"
    reasoning_effort: str = "omit"
    thinking: str = "omit"
    proxy_url: str = ""

    def __post_init__(self):
        u = urlsplit(self.base_url)
        if u.scheme not in {"http", "https"} or not u.netloc:
            raise ValueError("MODEL_BASE_URL must be an HTTP(S) URL")
        if u.username or u.password or u.query or u.fragment:
            raise ValueError("Do not embed credentials/query/fragment in MODEL_BASE_URL")
        if (not self.model or self.timeout_seconds <= 0
                or self.connect_timeout_seconds <= 0
                or self.min_request_interval_seconds < 0
                or self.max_completion_tokens <= 0):
            raise ValueError(
                "Model name, positive timeouts/max completion tokens, and a non-negative "
                "minimum request interval required"
            )
        if self.reasoning_effort not in {
            "none", "minimal", "low", "medium", "high", "xhigh", "max", "omit"
        }:
            raise ValueError(
                "MODEL_REASONING_EFFORT must be none, minimal, low, medium, high, "
                "xhigh, max or omit"
            )
        if self.thinking not in {"disabled", "enabled", "omit"}:
            raise ValueError("MODEL_THINKING must be disabled, enabled or omit")
        if self.token_limit_parameter not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError(
                "MODEL_TOKEN_LIMIT_PARAMETER must be max_tokens or max_completion_tokens"
            )
        if self.proxy_url:
            proxy = urlsplit(self.proxy_url)
            if (proxy.scheme not in {"http", "https"} or not proxy.netloc
                    or proxy.username or proxy.password or proxy.query or proxy.fragment):
                raise ValueError(
                    "MODEL_PROXY_URL must be an HTTP(S) URL without credentials/query/fragment"
                )

    @classmethod
    def from_env(cls):
        base_url = os.environ.get("MODEL_BASE_URL", "")
        base_scheme = urlsplit(base_url).scheme
        env_proxy = (
            os.environ.get("HTTPS_PROXY", "") if base_scheme == "https"
            else os.environ.get("HTTP_PROXY", "")
        )
        return cls(
            base_url=base_url,
            model=os.environ.get("MODEL_NAME", ""),
            api_key=os.environ.get("MODEL_API_KEY", ""),
            timeout_seconds=float(os.environ.get("MODEL_TIMEOUT_SECONDS", "60")),
            connect_timeout_seconds=float(
                os.environ.get("MODEL_CONNECT_TIMEOUT_SECONDS", "10")
            ),
            min_request_interval_seconds=float(
                os.environ.get("MODEL_MIN_REQUEST_INTERVAL_SECONDS", "0")
            ),
            temperature=float(os.environ.get("MODEL_TEMPERATURE", "0")),
            max_completion_tokens=int(os.environ.get(
                "MODEL_MAX_COMPLETION_TOKENS",
                os.environ.get("MODEL_MAX_TOKENS", "400"),
            )),
            token_limit_parameter=os.environ.get(
                "MODEL_TOKEN_LIMIT_PARAMETER", "max_completion_tokens"
            ),
            reasoning_effort=os.environ.get("MODEL_REASONING_EFFORT", "omit"),
            thinking=os.environ.get("MODEL_THINKING", "omit"),
            proxy_url=os.environ.get("MODEL_PROXY_URL", env_proxy),
        )

    def public(self):
        return {k: v for k, v in asdict(self).items() if k != "api_key"} | {
            "automatic_retries": 0,
            "response_format": {"type": "json_object"},
        }


class ModelCallError(RuntimeError):
    def __init__(self, metadata):
        super().__init__(metadata["error"])
        self.metadata = metadata


class ChatModel:
    def __init__(self, config: ModelConfig, *, transport=None, prompt_path=None):
        self.config = config
        self.prompt = Path(prompt_path or Path(__file__).resolve().parents[2] / "prompts" / "system.txt").read_text(encoding="utf-8")
        # httpx does not retry HTTP status codes; transport retries=0 prevents
        # connection retries. follow_redirects=False prevents a second request.
        # Configure the proxy on the transport itself so the experiment does
        # not depend on whether the launching shell propagated proxy variables.
        model_transport = transport or httpx.HTTPTransport(
            retries=0,
            proxy=config.proxy_url or None,
        )
        self.client = httpx.Client(
            transport=model_transport,
            timeout=httpx.Timeout(
                config.timeout_seconds,
                connect=config.connect_timeout_seconds,
            ),
            follow_redirects=False,
            trust_env=False,
        )
        self._last_request_started = None

    def close(self):
        self.client.close()

    def _redact(self, value):
        if isinstance(value, str):
            return value.replace(self.config.api_key, "[REDACTED]") if self.config.api_key else value
        if isinstance(value, dict):
            return {k: self._redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact(v) for v in value]
        return value

    def complete(self, payload):
        """Exactly one HTTP request; errors preserve counted-call metadata."""
        body = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": self.prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, allow_nan=False)},
            ],
            "temperature": self.config.temperature,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        body[self.config.token_limit_parameter] = self.config.max_completion_tokens
        if self.config.reasoning_effort != "omit":
            body["reasoning_effort"] = self.config.reasoning_effort
        if self.config.thinking != "omit":
            body["thinking"] = {"type": self.config.thinking}
        metadata = {
            "called": True, "raw_output": None, "elapsed_ms": 0.0,
            "usage": None, "config": self.config.public(), "error": None,
        }
        if self._last_request_started is not None:
            remaining = (
                self.config.min_request_interval_seconds
                - (time.monotonic() - self._last_request_started)
            )
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_started = time.monotonic()
        started = time.perf_counter()
        try:
            headers = {"Content-Type": "application/json"}
            if self.config.api_key:
                headers["Authorization"] = "Bearer " + self.config.api_key
            response = self.client.post(
                self.config.base_url.rstrip("/") + "/chat/completions",
                headers=headers, json=body,
            )
            metadata["http_status"] = response.status_code
            response.raise_for_status()
            data = response.json()
            # A provider may return usage even when content is null (e.g. output
            # exhaustion or refusal). Keep all available cost metadata before
            # validating the message so failed calls do not appear cost-free.
            usage = data.get("usage")
            metadata["usage"] = self._redact(usage) if isinstance(usage, dict) else None
            metadata["response_model"] = self._redact(data.get("model"))
            choice = data["choices"][0]
            metadata["finish_reason"] = self._redact(choice.get("finish_reason"))
            message = choice["message"]
            metadata["refusal"] = self._redact(message.get("refusal"))
            output = message.get("content")
            metadata["raw_output"] = self._redact(output)
            if not isinstance(output, str):
                raise ValueError("Model content must be a string")
            return metadata
        except Exception as exc:
            # Do not log headers/request URLs/response bodies on transport errors.
            # A status code and exception class suffice to diagnose credential and
            # protocol failures without leaking provider error-body secrets.
            metadata["error"] = f"{type(exc).__name__}: model request failed"
            raise ModelCallError(metadata) from exc
        finally:
            metadata["elapsed_ms"] = (time.perf_counter() - started) * 1000
