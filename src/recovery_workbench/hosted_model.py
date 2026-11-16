"""The hosted model: one OpenAI-compatible chat-completions API (owner answer to Q46).

``POST {WORKBENCH_HOSTED_URL}/chat/completions`` with ``Authorization: Bearer <key>``. ``diagnosis.diagnose`` drives
this client exactly as it drives Ollama: the same prompt (``PROMPT_VERSION``), per-packet schema, server-side
validation, policies and single repair call. Only the transport differs:

* how the request asks for JSON is a setting (``WORKBENCH_HOSTED_RESPONSE_FORMAT``), because providers differ:
  ``json_schema_strict`` sends the per-packet schema with ``strict: true``, ``json_schema`` sends it without,
  ``json_object`` asks for any JSON object and ``none`` sends no ``response_format``, leaving JSON to the prompt. The
  server-side validator judges the answer in every case. The schema is one object whose properties are all required,
  with ``additionalProperties: false``, as strict modes require; it is never changed per provider;
* temperature 0 unless ``WORKBENCH_HOSTED_TEMPERATURE`` says otherwise, or ``none`` leaves it out; ``reasoning_effort``
  only when ``WORKBENCH_HOSTED_REASONING_EFFORT`` is set. Ollama's ``think`` switch has no counterpart in this
  protocol, so a hosted diagnosis records thinking off;
* only ``choices[0].message.content`` is read. A ``reasoning`` or ``reasoning_content`` field is ignored, never
  stored and never shown; the ``usage`` token counts are kept per call, and ``finish_reason`` only when it is one of
  the documented values (``FINISH_REASONS``);
* every call, a repair included, takes one unit of the process's daily budget (``HostedCallBudget``) before it is
  sent. A provider's own refusal, a 429 rate limit included, is a failed call like any other;
* TLS is verified, redirects are not followed and the environment's proxy and netrc settings are ignored, so the key
  goes to the configured host and nowhere else. Connecting takes at most ``CONNECT_TIMEOUT_S`` and the whole reply
  must arrive within the diagnosis deadline; the reply is capped at ``MAX_RESPONSE_BYTES`` and must be strict JSON.

A hosted provider reports no model digest, so a hosted diagnosis records the provider host instead, and a hosted
answer is never recorded for replay: recorded mode replays local outputs only.

Any text a provider sends back that reaches a diagnosis's reasons is scrubbed of the key, cut to
``PROVIDER_TEXT_CHARS`` and escaped with ``report.visible``, so a terminal or a page shows it inert.
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from types import TracebackType
from urllib.parse import urlsplit

import httpx
from pydantic import JsonValue

from recovery_workbench import report
from recovery_workbench.contracts import ContractError
from recovery_workbench.diagnosis import ChatReply, ChatRequest, ModelCallError
from recovery_workbench.settings import (
    DEFAULT_HOSTED_RESPONSE_FORMAT,
    DEFAULT_HOSTED_TEMPERATURE,
    HOSTED_API_KEY,
    HOSTED_MODEL_NAME,
    HOSTED_REASONING_EFFORTS,
    HOSTED_RESPONSE_FORMATS,
    MAX_HOSTED_TEMPERATURE,
    Secret,
    Settings,
    hosted_url_problem,
    masked_url,
)

CHAT_PATH = "/chat/completions"
# The finish reasons OpenAI-compatible APIs document. Only these are kept: diagnosis._parse_reply quotes the reason in a
# diagnosis's reasons, and any other value is provider text that has not been through _provider_text.
FINISH_REASONS = frozenset({"stop", "length", "content_filter", "tool_calls"})
CONNECT_TIMEOUT_S = 5.0
# A diagnosis reply is a few kilobytes; this leaves room for usage blocks and long refusals, not for a flood.
MAX_RESPONSE_BYTES = 256 * 1024
PROVIDER_TEXT_CHARS = 300
# A provider that quotes this many consecutive characters of the key quotes part of it; its text is then not shown.
KEY_ECHO_CHARS = 12
RESPONSE_SCHEMA_NAME = "diagnosis"


class HostedCallBudget:
    """At most ``limit`` hosted calls per UTC day in this process. Every call counts, a repair included, and a call is
    counted before it is sent, so one that times out has still cost its unit."""

    def __init__(self, limit: int, *, clock: Callable[[], float] = time.time) -> None:
        if limit < 1:
            raise ValueError(f"a hosted call budget needs at least 1 call a day, got {limit}")
        self.limit = limit
        self._clock = clock
        self._lock = threading.Lock()
        self._day: date | None = None
        self._used = 0

    def take(self) -> None:
        """Count one call, or raise ``ModelCallError`` when today's calls are used up; nothing is sent then."""
        today = datetime.fromtimestamp(self._clock(), tz=UTC).date()
        with self._lock:
            if today != self._day:
                self._day, self._used = today, 0
            if self._used >= self.limit:
                raise ModelCallError(
                    f"the hosted model's daily call budget is used up: {self.limit} calls on {today.isoformat()} "
                    "(UTC), set by WORKBENCH_HOSTED_MAX_CALLS_PER_DAY; no call was sent. It renews at 00:00 UTC; "
                    "diagnose with the rules baseline meanwhile"
                )
            self._used += 1

    def used_today(self) -> int:
        today = datetime.fromtimestamp(self._clock(), tz=UTC).date()
        with self._lock:
            return self._used if today == self._day else 0


class HostedClient:
    """A minimal synchronous client for one OpenAI-compatible endpoint; ``diagnosis.diagnose`` calls ``chat``."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: Secret,
        budget: HostedCallBudget,
        response_format: str = DEFAULT_HOSTED_RESPONSE_FORMAT,
        temperature: float | None = DEFAULT_HOSTED_TEMPERATURE,
        reasoning_effort: str | None = None,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        problem = hosted_url_problem(base_url)
        if problem is not None:
            raise ContractError(f"the hosted model URL {problem}; got {masked_url(base_url)!r}")
        if not HOSTED_MODEL_NAME.fullmatch(model):
            raise ContractError(f"{model[:80]!r} is not a hosted model ID such as gpt-4o-mini")
        if not HOSTED_API_KEY.fullmatch(api_key.reveal()):
            raise ContractError("the hosted model's API key must be 8 to 512 printable ASCII characters with no spaces")
        if response_format not in HOSTED_RESPONSE_FORMATS:
            raise ContractError(f"the hosted response format must be one of {sorted(HOSTED_RESPONSE_FORMATS)}")
        if temperature is not None and not 0.0 <= temperature <= MAX_HOSTED_TEMPERATURE:
            raise ContractError(f"the hosted temperature must be from 0 to {MAX_HOSTED_TEMPERATURE:g}, or None")
        if reasoning_effort is not None and reasoning_effort not in HOSTED_REASONING_EFFORTS:
            raise ContractError(f"the reasoning effort must be one of {sorted(HOSTED_REASONING_EFFORTS)}, or None")
        self.model = model
        self.provider_host = str(urlsplit(base_url).hostname)  # hosted_url_problem has checked there is one
        self.endpoint = base_url.rstrip("/") + CHAT_PATH
        self._key = api_key
        self._budget = budget
        self._response_format = response_format
        self._temperature = temperature
        self._reasoning_effort = reasoning_effort
        self._max_response_bytes = max_response_bytes
        # follow_redirects=False keeps the key off any other host; trust_env=False ignores HTTP(S)_PROXY, ALL_PROXY,
        # NO_PROXY, SSL_CERT_FILE and .netrc, which would otherwise route the request, or add credentials to it.
        self._http = httpx.Client(transport=transport, follow_redirects=False, verify=True, trust_env=False)

    def __enter__(self) -> HostedClient:
        return self

    def __exit__(
        self, kind: type[BaseException] | None, problem: BaseException | None, trace: TracebackType | None
    ) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def chat(self, request: ChatRequest, *, timeout_s: float) -> ChatReply:
        self._budget.take()
        headers = {"Authorization": f"Bearer {self._key.reveal()}", "Accept": "application/json",
                   "Accept-Encoding": "identity"}
        deadline = time.monotonic() + timeout_s
        timeout = httpx.Timeout(timeout_s, connect=min(CONNECT_TIMEOUT_S, timeout_s))
        try:
            with self._http.stream("POST", self.endpoint, json=self.request_body(request), headers=headers,
                                   timeout=timeout) as response:
                status = response.status_code
                if 300 <= status < 400:
                    raise ModelCallError(
                        f"the hosted model answered HTTP {status}, a redirect, which is not followed so that the key "
                        "goes to the configured host only; check WORKBENCH_HOSTED_URL"
                    )
                content_type = response.headers.get("content-type", "")
                retry_after = response.headers.get("retry-after", "")
                raw = self._read_capped(response, deadline, timeout_s)
        except httpx.TimeoutException:
            raise ModelCallError(f"the hosted model did not answer within {timeout_s:.1f} s") from None
        except httpx.HTTPError as failed:
            # from None: the request's own exception chain is not kept beside the message.
            raise ModelCallError(f"the hosted model call failed: {self._provider_text(repr(failed))}") from None
        if status != 200:
            waited = f" (Retry-After: {self._provider_text(retry_after)})" if retry_after else ""
            raise ModelCallError(f"the hosted model answered HTTP {status}{waited}: "
                                 f"{self._provider_text(raw.decode('utf-8', errors='replace'))}")
        return self._reply(content_type, raw)

    def request_body(self, request: ChatRequest) -> dict[str, JsonValue]:
        """The chat-completions request for ``request``: only the fields the settings ask for are present."""
        body: dict[str, JsonValue] = {
            "model": self.model,
            "messages": [message.model_dump() for message in request.messages],
            "stream": False,
        }
        if self._temperature is not None:
            body["temperature"] = self._temperature
        if self._reasoning_effort is not None:
            body["reasoning_effort"] = self._reasoning_effort
        if self._response_format == "json_object":
            body["response_format"] = {"type": "json_object"}
        elif self._response_format in ("json_schema", "json_schema_strict"):
            schema: dict[str, JsonValue] = {"name": RESPONSE_SCHEMA_NAME, "schema": request.response_schema}
            if self._response_format == "json_schema_strict":
                schema["strict"] = True
            body["response_format"] = {"type": "json_schema", "json_schema": schema}
        return body

    def _read_capped(self, response: httpx.Response, deadline: float, timeout_s: float) -> bytes:
        """The reply's bytes, as sent: at most the size cap, and all of them before the deadline."""
        encoding = response.headers.get("content-encoding", "identity")
        if encoding.strip().lower() not in ("", "identity"):
            # A compressed body could grow far past the cap once decoded, so it is refused unread. The value is quoted
            # as the provider sent it, scrubbed: lowercased first, an echoed key would no longer match the scrub.
            raise ModelCallError(f"the hosted model sent a {self._provider_text(encoding)!r}-encoded reply although "
                                 "none was asked for; it was not read")
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > self._max_response_bytes:
            raise ModelCallError(f"the hosted model's reply declares {declared} bytes, over the "
                                 f"{self._max_response_bytes}-byte limit; it was not read")
        received = bytearray()
        for chunk in response.iter_raw():
            received += chunk
            if len(received) > self._max_response_bytes:
                raise ModelCallError(f"the hosted model's reply passed the {self._max_response_bytes}-byte limit; "
                                     "the rest was not read")
            if time.monotonic() > deadline:
                raise ModelCallError(f"the hosted model's reply did not finish within {timeout_s:.1f} s")
        return bytes(received)

    def _reply(self, content_type: str, raw: bytes) -> ChatReply:
        text = raw.decode("utf-8", errors="replace")
        media_type = content_type.split(";", 1)[0].strip()  # not lowercased: _provider_text must see it as sent
        if media_type.lower() != "application/json":
            raise ModelCallError(f"the hosted model's reply is {self._provider_text(media_type) or 'untyped'}, not "
                                 f"application/json: {self._provider_text(text)}")
        payload = self._strict_json(raw)
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            # An error a provider sends with status 200, such as a JSON mode it could not meet, is quoted here.
            raise ModelCallError(f"the hosted model's reply has no choices: {self._provider_text(text)}")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ModelCallError("the hosted model's reply has no message in its first choice")
        content = message.get("content")
        if not isinstance(content, str):
            refusal = message.get("refusal")
            if isinstance(refusal, str):
                raise ModelCallError(f"the hosted model refused to answer: {self._provider_text(refusal)}")
            raise ModelCallError("the hosted model's reply has no message content")
        usage = payload.get("usage")
        counts = usage if isinstance(usage, dict) else {}
        return ChatReply(
            content=content,
            model=_optional_str(payload.get("model")),
            done_reason=_finish_reason(choice.get("finish_reason")),
            prompt_eval_count=_optional_int(counts.get("prompt_tokens")),
            eval_count=_optional_int(counts.get("completion_tokens")),
        )

    def _strict_json(self, raw: bytes) -> dict[str, JsonValue]:
        """The reply as one JSON object: UTF-8, no NaN or Infinity, and no key twice in one object."""
        def no_constant(name: str) -> object:
            raise ValueError(f"{name} is not JSON")

        def unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
            keys = [key for key, _ in pairs]
            if len(set(keys)) != len(keys):
                raise ValueError("one object names a key twice")
            return dict(pairs)

        try:
            payload = json.loads(raw.decode("utf-8"), parse_constant=no_constant, object_pairs_hook=unique_keys)
        except ValueError as invalid:  # UnicodeDecodeError and JSONDecodeError are ValueErrors
            problem = self._provider_text(str(invalid))
            raise ModelCallError(f"the hosted model's reply is not strict JSON: {problem}") from None
        except RecursionError:
            # Python's reader stops some thousands of levels deep, well inside the size cap. RecursionError is no
            # ValueError, and uncaught it would end the job with an unexpected error instead of needs_review.
            raise ModelCallError("the hosted model's reply nests JSON too deeply to read; it was not used") from None
        if not isinstance(payload, dict):
            raise ModelCallError(f"the hosted model's reply is a JSON {type(payload).__name__}, not an object")
        return payload

    def _provider_text(self, text: str) -> str:
        """Text the provider wrote, fit for a diagnosis's reasons: no key, one line, cut short and escaped."""
        key = self._key.reveal()
        line = " ".join(text.replace(key, "<api key>").split())
        shortened = line if len(line) <= PROVIDER_TEXT_CHARS else line[:PROVIDER_TEXT_CHARS] + "..."
        echo = min(KEY_ECHO_CHARS, len(key))
        if any(key[start:start + echo] in shortened for start in range(len(key) - echo + 1)):
            return "<the reply quotes part of the API key, so it is not shown>"
        return report.visible(shortened)


def client_from_settings(configured: Settings, budget: HostedCallBudget) -> HostedClient:
    """The hosted client the settings describe; refused when hosted mode is not configured."""
    hosted = configured.hosted
    if hosted.url is None or hosted.model is None or hosted.api_key is None:
        raise ContractError(
            f"hosted mode is not configured on this server: {', '.join(hosted.missing())} not set. Choose local, "
            "recorded or rules"
        )
    return HostedClient(base_url=hosted.url, model=hosted.model, api_key=hosted.api_key, budget=budget,
                        response_format=hosted.response_format, temperature=hosted.temperature,
                        reasoning_effort=hosted.reasoning_effort)


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _finish_reason(value: object) -> str | None:
    """``finish_reason`` when it is one of FINISH_REASONS; None for anything else, an echoed key included."""
    return value if isinstance(value, str) and value in FINISH_REASONS else None
