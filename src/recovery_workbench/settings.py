"""Runtime settings read from WORKBENCH_* environment variables (see .env.example)."""
from __future__ import annotations

import hmac
import ipaddress
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse, urlsplit

# A stored mode (contracts.ModelMode), or auto, which a process settles into one of them once, when it starts
# (model_source.resolve).
MODEL_MODES = frozenset({"local", "recorded", "rules", "hosted", "auto"})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# A model name as Ollama writes one, [host/][namespace/]model[:tag], in ASCII letters, digits and _ . : / - only. The
# model ID is stored with every model diagnosis and shown in reports and receipts.
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/\-]{0,127}")  # ASCII on purpose: \w would also match non-ASCII letters


# --------------------------------------------------------------------------- the hosted model (owner answer to Q46)

# An OpenAI-compatible chat-completions API: hosted mode uses it, and auto does when no local model answers. Every
# variable below is checked at start whenever it is set, whatever the mode. The API key is a secret: it leaves this
# process only in the Authorization header sent to WORKBENCH_HOSTED_URL's host (hosted_model.HostedClient).
# How the request asks for JSON, because providers differ: json_schema_strict sends the diagnosis schema with strict
# true (the only form Anthropic's layer takes), json_schema sends it without, json_object asks for any JSON object, and
# none sends no response_format, leaving it to the prompt. The server-side validator judges the answer in every case.
HOSTED_RESPONSE_FORMATS = frozenset({"json_schema_strict", "json_schema", "json_object", "none"})
DEFAULT_HOSTED_RESPONSE_FORMAT = "json_schema"
DEFAULT_HOSTED_MAX_CALLS_PER_DAY = 50
# Temperature 0 by default, as for Ollama. Some providers refuse a value below 1 or need none at all with reasoning on,
# so the word NOT_SENT leaves the field out of the request.
DEFAULT_HOSTED_TEMPERATURE = 0.0
NOT_SENT = "none"
MAX_HOSTED_TEMPERATURE = 2.0
# Sent as reasoning_effort only when set. Reasoning models default to medium, which costs output tokens on every call.
HOSTED_REASONING_EFFORTS = frozenset({"none", "low", "medium", "high"})
# A hosted model ID as providers write them (@cf/openai/gpt-oss-20b on Cloudflare Workers AI, gpt-4o-mini,
# meta-llama/Llama-3.1-8B-Instruct): MODEL_NAME's characters and @, at most 128. No whitespace, control character or
# quote can pass, because the ID is stored and shown like an Ollama model name.
HOSTED_MODEL_NAME = re.compile(r"[A-Za-z0-9@][A-Za-z0-9_.:/@\-]{0,127}")  # NOSONAR(S6353) ASCII, as MODEL_NAME
# Printable ASCII without spaces, so the key can only ever be one Authorization header value.
HOSTED_API_KEY = re.compile(r"[!-~]{8,512}")
_HOSTED_VARIABLES = ("WORKBENCH_HOSTED_URL", "WORKBENCH_HOSTED_MODEL", "WORKBENCH_HOSTED_API_KEY")
_CHAT_COMPLETIONS = "/chat/completions"
# Characters a URL cannot hold unencoded (RFC 3986). One in the hosted URL is most likely a <placeholder> left in it.
_NOT_IN_URLS = frozenset('<>"{}|\\^`')


class Secret:
    """A secret from the environment. Only ``reveal()`` gives its text: str(), repr() and f-strings show ``<hidden>``,
    so a settings object, a log line or an exception that holds one cannot print it."""

    __slots__ = ("_text",)

    def __init__(self, text: str) -> None:
        self._text = text

    def reveal(self) -> str:
        return self._text

    def __repr__(self) -> str:
        return "Secret(<hidden>)"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and hmac.compare_digest(self._text.encode(), other._text.encode())

    def __hash__(self) -> int:
        return hash(self._text)


@dataclass(frozen=True)
class HostedSettings:
    """The hosted model's settings; None marks a variable that is not set."""

    url: str | None = None
    model: str | None = None
    api_key: Secret | None = field(default=None, repr=False)
    max_calls_per_day: int = DEFAULT_HOSTED_MAX_CALLS_PER_DAY  # per process, every call counted, repairs included
    response_format: str = DEFAULT_HOSTED_RESPONSE_FORMAT
    temperature: float | None = DEFAULT_HOSTED_TEMPERATURE  # None: no temperature in the request
    reasoning_effort: str | None = None  # None: no reasoning_effort in the request

    def missing(self) -> tuple[str, ...]:
        """The variables hosted mode still needs: all three of URL, model and key must be set."""
        values = (self.url, self.model, self.api_key)
        return tuple(name for name, value in zip(_HOSTED_VARIABLES, values, strict=True) if value is None)

    @property
    def configured(self) -> bool:
        return not self.missing()

    @property
    def host(self) -> str | None:
        """The provider host the key is sent to; a hosted diagnosis records it."""
        return None if self.url is None else urlsplit(self.url).hostname


def hosted_url_problem(url: str) -> str | None:
    """Why ``url`` cannot be the hosted model's base URL, or None: https, or plain http to a loopback host only (a local
    OpenAI-compatible server), with a host and no user name, password, query or fragment."""
    if not url.isascii() or not url.isprintable() or any(character.isspace() for character in url):
        return "must be printable ASCII with no spaces"
    if any(character in _NOT_IN_URLS for character in url):
        return "holds a character a URL cannot hold, such as < or >; replace any <placeholder> in it"
    try:
        parts = urlsplit(url)
        _ = parts.port  # a port that is not a number from 0 to 65535 raises here
    except ValueError:
        return "is not a well-formed URL"
    if "@" in parts.netloc:
        return "must not carry a user name or password; the key goes in WORKBENCH_HOSTED_API_KEY"
    if "?" in url or "#" in url:
        return "must not carry a query or a fragment"
    if parts.scheme not in ("https", "http") or not parts.hostname:
        return "must be an https URL with a host"
    if parts.scheme == "http" and parts.hostname not in LOOPBACK_HOSTS:
        return "must use https; plain http is allowed only to a loopback host"
    if parts.path.rstrip("/").endswith(_CHAT_COMPLETIONS):
        return f"is the API's base URL, without {_CHAT_COMPLETIONS}, which the workbench adds"
    return None


def masked_url(url: str) -> str:
    """``url`` as an error message may show it: a user name, password, query or fragment becomes <hidden>."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<not a URL; not shown>"
    netloc = parts.netloc if "@" not in parts.netloc else "<hidden>@" + parts.netloc.rsplit("@", 1)[1]
    query = "?<hidden>" if "?" in url else ""
    fragment = "#<hidden>" if "#" in url else ""
    return f"{parts.scheme}://{netloc}{parts.path}{query}{fragment}"[:200]


def _optional(source: Mapping[str, str], name: str) -> str | None:
    """A variable's value, or None when it is unset or empty (as .env.example lists the hosted ones)."""
    value = source.get(name, "")
    return value if value != "" else None


def _hosted_settings(source: Mapping[str, str]) -> HostedSettings:
    url = _optional(source, "WORKBENCH_HOSTED_URL")
    if url is not None and (problem := hosted_url_problem(url)) is not None:
        raise ValueError(
            f"WORKBENCH_HOSTED_URL {problem}; got {masked_url(url)!r}. Give the provider's base URL, such as "
            "https://<provider-host>/v1, or http://127.0.0.1:<port>/v1 for a local OpenAI-compatible server"
        )
    model = _optional(source, "WORKBENCH_HOSTED_MODEL")
    if model is not None and not HOSTED_MODEL_NAME.fullmatch(model):
        raise ValueError(
            "WORKBENCH_HOSTED_MODEL must be a model ID such as @cf/openai/gpt-oss-20b or gpt-4o-mini: ASCII letters, "
            f"digits and _ . : / @ -, at most 128 characters, no spaces or quotes; got {model[:80]!r}"
        )
    key = _optional(source, "WORKBENCH_HOSTED_API_KEY")
    if key is not None and not HOSTED_API_KEY.fullmatch(key):
        raise ValueError(  # never the key itself, nor any part of it
            "WORKBENCH_HOSTED_API_KEY must be 8 to 512 printable ASCII characters with no spaces or line breaks; got "
            f"{len(key)} characters, not shown. Copy the key from the provider again, without quotes or a newline"
        )
    response_format = source.get("WORKBENCH_HOSTED_RESPONSE_FORMAT", DEFAULT_HOSTED_RESPONSE_FORMAT)
    if response_format not in HOSTED_RESPONSE_FORMATS:
        raise ValueError(
            f"WORKBENCH_HOSTED_RESPONSE_FORMAT must be one of {sorted(HOSTED_RESPONSE_FORMATS)}, got "
            f"{response_format[:40]!r}; use the strongest form your provider accepts (json_schema_strict, then "
            "json_schema, json_object, none)"
        )
    reasoning_effort = _optional(source, "WORKBENCH_HOSTED_REASONING_EFFORT")
    if reasoning_effort is not None and reasoning_effort not in HOSTED_REASONING_EFFORTS:
        raise ValueError(
            f"WORKBENCH_HOSTED_REASONING_EFFORT must be one of {sorted(HOSTED_REASONING_EFFORTS)}, or unset to send "
            f"none; got {reasoning_effort[:40]!r}"
        )
    return HostedSettings(
        url=url, model=model, api_key=None if key is None else Secret(key),
        max_calls_per_day=_parse_positive_int(
            source.get("WORKBENCH_HOSTED_MAX_CALLS_PER_DAY", str(DEFAULT_HOSTED_MAX_CALLS_PER_DAY)),
            "WORKBENCH_HOSTED_MAX_CALLS_PER_DAY",
        ),
        response_format=response_format,
        temperature=_hosted_temperature(source.get("WORKBENCH_HOSTED_TEMPERATURE", "0")),
        reasoning_effort=reasoning_effort,
    )


def _hosted_temperature(raw: str) -> float | None:
    if raw == NOT_SENT:
        return None
    try:
        temperature = float(raw)
    except ValueError:
        temperature = math.nan
    if not 0.0 <= temperature <= MAX_HOSTED_TEMPERATURE:  # NaN fails this too
        raise ValueError(
            f"WORKBENCH_HOSTED_TEMPERATURE must be a number from 0 to {MAX_HOSTED_TEMPERATURE:g}, or {NOT_SENT} to "
            f"send no temperature (for a provider that refuses one); got {raw[:40]!r}"
        )
    return temperature


# --------------------------------------------------------------------------- all settings


@dataclass(frozen=True)
class Settings:
    environment: str
    model_mode: str  # a MODEL_MODES value; a process settles auto into a stored mode once, at start
    model: str
    model_think: bool  # M0 passed with qwen3:8b both ways; M4's dev split picks the frozen-test setting
    ollama_url: str
    hosted: HostedSettings  # the hosted model block above
    allow_external_actions: bool
    model_max_calls: int
    tool_max_calls: int
    run_timeout_seconds: int
    data_dir: Path
    # One source of truth for recovery timing, shared by planner, executor and the scenario oracle.
    plan_ttl_seconds: int
    approval_ttl_seconds: int
    stale_after_seconds: int


def _parse_flag(raw: str, name: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in {"1", "true", "yes"}:
        return True
    if lowered in {"0", "false", "no"}:
        return False
    raise ValueError(f"{name} must be true or false, got {raw!r}")


def _parse_positive_int(raw: str, name: str) -> int:
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}")
    return parsed


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Read and validate settings; refuse anything that would leave the synthetic sandbox."""
    source = os.environ if env is None else env
    settings = Settings(
        environment=source.get("WORKBENCH_ENV", "synthetic"),
        model_mode=source.get("WORKBENCH_MODEL_MODE", "local"),
        model=source.get("WORKBENCH_MODEL", "qwen3:8b"),
        model_think=_parse_flag(source.get("WORKBENCH_MODEL_THINK", "false"), "WORKBENCH_MODEL_THINK"),
        ollama_url=source.get("WORKBENCH_OLLAMA_URL", "http://127.0.0.1:11434"),
        hosted=_hosted_settings(source),
        allow_external_actions=_parse_flag(
            source.get("WORKBENCH_ALLOW_EXTERNAL_ACTIONS", "false"), "WORKBENCH_ALLOW_EXTERNAL_ACTIONS"
        ),
        model_max_calls=_parse_positive_int(source.get("WORKBENCH_MODEL_MAX_CALLS", "4"), "WORKBENCH_MODEL_MAX_CALLS"),
        tool_max_calls=_parse_positive_int(source.get("WORKBENCH_TOOL_MAX_CALLS", "6"), "WORKBENCH_TOOL_MAX_CALLS"),
        run_timeout_seconds=_parse_positive_int(
            source.get("WORKBENCH_RUN_TIMEOUT_SECONDS", "90"), "WORKBENCH_RUN_TIMEOUT_SECONDS"
        ),
        data_dir=Path(source.get("WORKBENCH_DATA_DIR", "./local_data")),
        plan_ttl_seconds=_parse_positive_int(
            source.get("WORKBENCH_PLAN_TTL_SECONDS", "1800"), "WORKBENCH_PLAN_TTL_SECONDS"
        ),
        approval_ttl_seconds=_parse_positive_int(
            source.get("WORKBENCH_APPROVAL_TTL_SECONDS", "900"), "WORKBENCH_APPROVAL_TTL_SECONDS"
        ),
        stale_after_seconds=_parse_positive_int(
            source.get("WORKBENCH_STALE_AFTER_SECONDS", "3600"), "WORKBENCH_STALE_AFTER_SECONDS"
        ),
    )
    if settings.environment != "synthetic":
        raise ValueError(
            f"WORKBENCH_ENV must be 'synthetic', got {settings.environment!r}; this workbench has no live mode"
        )
    if settings.allow_external_actions:
        raise ValueError("WORKBENCH_ALLOW_EXTERNAL_ACTIONS must be false; v1 has no external actions")
    if settings.model_mode not in MODEL_MODES:
        raise ValueError(f"WORKBENCH_MODEL_MODE must be one of {sorted(MODEL_MODES)}, got {settings.model_mode!r}")
    if settings.model_mode == "hosted" and not settings.hosted.configured:
        raise ValueError(
            f"WORKBENCH_MODEL_MODE=hosted needs {', '.join(settings.hosted.missing())} set; set all three, or choose "
            "local, recorded, rules or auto"
        )
    if not MODEL_NAME.fullmatch(settings.model):
        raise ValueError(
            "WORKBENCH_MODEL must be an Ollama model name such as qwen3:8b: ASCII letters, digits and _ . : / -, "
            f"starting with a letter or digit, at most 128 characters; got {settings.model!r}"
        )
    host = urlparse(settings.ollama_url).hostname
    if host not in LOOPBACK_HOSTS:
        raise ValueError(
            f"WORKBENCH_OLLAMA_URL must point at a loopback host, got {settings.ollama_url!r}; "
            "the model listener stays local"
        )
    return settings


# --------------------------------------------------------------------------- public mode
# docs/DEMO_HOSTING.md, section 3.3: what lets the server run behind one private demo link. Only api.main reads these,
# through load_public_settings, and public mode is off unless WORKBENCH_PUBLIC_ORIGIN is set. load_settings never reads
# them, so the CLI, the evaluation and the API with public mode off see exactly what they saw before. The image build
# also reads WORKBENCH_PUBLIC_SEEDS, through published_seeds, to seed the demo runs it holds (public_template, Q48).

_DNS_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
# A lowercase DNS name of two labels or more, as a browser writes it in Origin: no wildcard and no trailing dot.
PUBLIC_HOST_NAME = re.compile(rf"{_DNS_LABEL}(?:\.{_DNS_LABEL})+")
_HTTPS = "https://"
_ORIGIN_PORT = re.compile(r"[1-9][0-9]{0,4}")  # NOSONAR(S6353) ASCII digits: \d also matches others
_DEFAULT_HTTPS_PORT = 443
# The demo token's start check (security audit run 3). Login compares the right token before its failed-login window
# (Q38), so nothing limits guessing and the token must be random. No check can prove that, but these refuse the shapes
# a person picks: words, keyboard rows, alphabets and patterns. secrets.token_urlsafe(32) writes 43 characters from 64
# symbols. In four runs of 200,000 such tokens the check refused 108 to 141 a run (0.054 to 0.071 per cent, about 1 in
# 1,600): 477 of the 479 held no digit and 2 had too few letters of one case. The fewest different characters seen
# was 21.
MIN_DEMO_TOKEN_CHARACTERS = 43
MAX_DEMO_TOKEN_CHARACTERS = 256  # api.LoginRequest takes no longer token, so a longer one could never log in
DEMO_TOKEN_CHARACTERS = re.compile(r"[A-Za-z0-9_.~+/=\-]+")  # what token_urlsafe and base64 write
MIN_DEMO_TOKEN_UPPERCASE = 3
MIN_DEMO_TOKEN_LOWERCASE = 3
MIN_DEMO_TOKEN_DIGITS = 1
MIN_DEMO_TOKEN_DISTINCT = 20
# Any 8 characters that occur twice make the token a pattern; the check refused none of the tokens above for that.
DEMO_TOKEN_REPEAT = 8
DEMO_TOKEN_ADVICE = 'generate one with: python3 -c "import secrets; print(secrets.token_urlsafe(32))"'
MAX_PUBLIC_SEED = 2**31 - 1  # api.MAX_SEED: no run has a larger seed
DEFAULT_PUBLIC_SEEDS = "101"  # the README's demo seed
MIN_LISTEN_PORT = 1024  # the range api.main takes for --port
MAX_LISTEN_PORT = 65535
# Set without WORKBENCH_PUBLIC_ORIGIN, either says the operator meant public mode and mistyped or forgot its switch.
PUBLIC_ONLY_SETTINGS = ("WORKBENCH_DEMO_TOKEN", "WORKBENCH_PUBLIC_TRUSTED_PROXIES")


@dataclass(frozen=True)
class PublicSettings:
    origin: str  # the one origin viewers open, exactly as a browser sends it in Origin
    host: str  # the Host header that origin's requests carry
    demo_token: str = field(repr=False)  # a secret: in no repr, message or log line
    seeds: frozenset[int]  # the published seeds, the only ones a viewer may seed
    runs_per_session: int
    runs_per_process: int
    data_limit_bytes: int  # no new run once the data directory holds this much
    trusted_proxies: tuple[str, ...]  # empty: no forwarded header is believed and no per-address limit applies
    port: int | None  # $PORT, when the host sets one
    template_dir: Path | None  # the demo runs the image holds (Q48); None: every run is seeded when a viewer asks


def load_public_settings(env: Mapping[str, str] | None = None) -> PublicSettings | None:
    """Public mode's settings, validated, or None when ``WORKBENCH_PUBLIC_ORIGIN`` is unset or empty: public mode off.

    Every value is checked here, at start, so a server that would run with a weak protection does not start at all.
    No message quotes the demo token.
    """
    source = os.environ if env is None else env
    raw_origin = source.get("WORKBENCH_PUBLIC_ORIGIN", "")
    if not raw_origin:
        stray = ", ".join(name for name in PUBLIC_ONLY_SETTINGS if source.get(name))
        if stray:
            raise ValueError(
                f"{stray} is set but WORKBENCH_PUBLIC_ORIGIN is not, and public mode is off without it; set "
                f"WORKBENCH_PUBLIC_ORIGIN to the demo's https origin, or unset {stray}"
            )
        return None
    origin, host = _public_origin(raw_origin)
    runs_per_session = _parse_positive_int(
        source.get("WORKBENCH_PUBLIC_RUNS_PER_SESSION", "3"), "WORKBENCH_PUBLIC_RUNS_PER_SESSION"
    )
    runs_per_process = _parse_positive_int(
        source.get("WORKBENCH_PUBLIC_RUNS_PER_PROCESS", "30"), "WORKBENCH_PUBLIC_RUNS_PER_PROCESS"
    )
    if runs_per_session > runs_per_process:
        raise ValueError(
            f"WORKBENCH_PUBLIC_RUNS_PER_SESSION ({runs_per_session}) must not exceed WORKBENCH_PUBLIC_RUNS_PER_PROCESS "
            f"({runs_per_process}); lower the first or raise the second"
        )
    data_limit_mb = _parse_positive_int(
        source.get("WORKBENCH_PUBLIC_DATA_LIMIT_MB", "200"), "WORKBENCH_PUBLIC_DATA_LIMIT_MB"
    )
    return PublicSettings(
        origin=origin,
        host=host,
        demo_token=_demo_token(source.get("WORKBENCH_DEMO_TOKEN", "")),
        seeds=published_seeds(source),
        runs_per_session=runs_per_session,
        runs_per_process=runs_per_process,
        data_limit_bytes=data_limit_mb * 1024 * 1024,
        trusted_proxies=_trusted_proxies(source.get("WORKBENCH_PUBLIC_TRUSTED_PROXIES", "")),
        port=_listen_port(source.get("PORT", "")),
        template_dir=_template_dir(source.get("WORKBENCH_PUBLIC_TEMPLATE_DIR", "")),
    )


def published_seeds(env: Mapping[str, str] | None = None) -> frozenset[int]:
    """WORKBENCH_PUBLIC_SEEDS, validated: the seeds a viewer may seed, and the ones the image's demo runs are seeded with
    when it is built, so both read the one variable the same way."""
    source = os.environ if env is None else env
    return _public_seeds(source.get("WORKBENCH_PUBLIC_SEEDS", DEFAULT_PUBLIC_SEEDS))


def _public_origin(raw: str) -> tuple[str, str]:
    """WORKBENCH_PUBLIC_ORIGIN as the Origin a browser sends, and the Host header it sends with it."""
    problem = _public_origin_problem(raw)
    if problem is not None:
        raise ValueError(
            f"WORKBENCH_PUBLIC_ORIGIN must be the one https origin viewers open, such as https://<name>.<host>: "
            f"{problem}; got {raw!r}"
        )
    return raw, raw.removeprefix(_HTTPS)


def _public_origin_problem(raw: str) -> str | None:
    """Why ``raw`` is not exactly one https origin in the form a browser sends, or None."""
    if not raw.startswith(_HTTPS):
        return "it must start with https://, in lowercase, because public mode serves viewers over HTTPS only"
    rest = raw.removeprefix(_HTTPS)
    for mark, part in (("@", "the user information"), ("/", "the path, even a single /"), ("?", "the query"),
                       ("#", "the fragment")):
        if mark in rest:
            return f"remove {part}: an origin is the scheme, the host and an optional port, nothing else"
    name, colon, port = rest.partition(":")
    if "*" in name:
        return "a wildcard is never accepted; name exactly one host"
    if colon and not _ORIGIN_PORT.fullmatch(port):
        return "the port must be a number from 1 to 65535, without leading zeros"
    if colon and int(port) == _DEFAULT_HTTPS_PORT:
        return "remove :443, because a browser leaves the default port out of Origin and it would never match"
    if colon and int(port) > MAX_LISTEN_PORT:
        return "the port must be a number from 1 to 65535, without leading zeros"
    if len(name) > 253 or not PUBLIC_HOST_NAME.fullmatch(name):
        return "the host must be a lowercase DNS name of two labels or more, such as <name>.<host>"
    if name.rsplit(".", 1)[-1].isdigit():
        return "the host must be a DNS name, not an IP address"
    return None


def _demo_token(raw: str) -> str:
    """WORKBENCH_DEMO_TOKEN, refused unless it has the shape of secrets.token_urlsafe(32) or longer. No message quotes
    it."""
    if not raw:
        raise ValueError(
            f"WORKBENCH_DEMO_TOKEN must be set in public mode, from the host's secret store; {DEMO_TOKEN_ADVICE}"
        )
    problem = _demo_token_problem(raw)
    if problem is not None:
        raise ValueError(
            f"WORKBENCH_DEMO_TOKEN {problem}; got a value of {len(raw)} characters (not shown); {DEMO_TOKEN_ADVICE}"
        )
    return raw


def _demo_token_problem(raw: str) -> str | None:
    if not MIN_DEMO_TOKEN_CHARACTERS <= len(raw) <= MAX_DEMO_TOKEN_CHARACTERS:
        return f"must be {MIN_DEMO_TOKEN_CHARACTERS} to {MAX_DEMO_TOKEN_CHARACTERS} characters long"
    if not DEMO_TOKEN_CHARACTERS.fullmatch(raw):
        return "may hold only ASCII letters, digits and - _ . ~ + / ="
    uppercase = sum(character.isupper() for character in raw)  # ASCII only by now, so these count ASCII classes
    lowercase = sum(character.islower() for character in raw)
    digits = sum(character.isdigit() for character in raw)
    if (uppercase < MIN_DEMO_TOKEN_UPPERCASE or lowercase < MIN_DEMO_TOKEN_LOWERCASE
            or digits < MIN_DEMO_TOKEN_DIGITS):
        return (f"must hold at least {MIN_DEMO_TOKEN_UPPERCASE} uppercase letters, {MIN_DEMO_TOKEN_LOWERCASE} "
                f"lowercase letters and {MIN_DEMO_TOKEN_DIGITS} digit, as a random token does (about 1 generated token "
                "in 1,600 has no digit; draw again)")
    if len(set(raw)) < MIN_DEMO_TOKEN_DISTINCT:
        return f"must use at least {MIN_DEMO_TOKEN_DISTINCT} different characters to look random"
    pieces = [raw[start:start + DEMO_TOKEN_REPEAT] for start in range(len(raw) - DEMO_TOKEN_REPEAT + 1)]
    if len(set(pieces)) < len(pieces):
        return f"repeats a run of {DEMO_TOKEN_REPEAT} characters, so it is a pattern rather than random"
    return None


def _public_seeds(raw: str) -> frozenset[int]:
    """WORKBENCH_PUBLIC_SEEDS: the seeds a viewer may seed, separated by commas, each listed once."""
    texts = [item.strip() for item in raw.split(",")]
    if not all(re.fullmatch(r"[0-9]{1,10}", text) and int(text) <= MAX_PUBLIC_SEED for text in texts):  # NOSONAR(S6353) ASCII digits: \d also matches others
        raise ValueError(
            f"WORKBENCH_PUBLIC_SEEDS must list seeds from 0 to {MAX_PUBLIC_SEED}, separated by commas, such as 101; "
            f"got {raw!r}"
        )
    seeds = [int(text) for text in texts]
    if len(set(seeds)) < len(seeds):
        raise ValueError(f"WORKBENCH_PUBLIC_SEEDS lists a seed twice; got {raw!r}; list each seed once")
    return frozenset(seeds)


def _trusted_proxies(raw: str) -> tuple[str, ...]:
    """WORKBENCH_PUBLIC_TRUSTED_PROXIES: the proxies, as addresses or networks, whose X-Forwarded-For is believed."""
    if not raw:
        return ()
    proxies: list[str] = []
    for item in raw.split(","):
        text = item.strip()
        try:
            network = ipaddress.ip_network(text, strict=True)
        except ValueError as malformed:
            raise ValueError(
                f"WORKBENCH_PUBLIC_TRUSTED_PROXIES must list the host's proxy IP addresses or networks, such as "
                f"10.0.0.0/8, separated by commas, and never *; {text!r} is not one; got {raw!r}"
            ) from malformed
        if network.prefixlen == 0:
            raise ValueError(
                f"WORKBENCH_PUBLIC_TRUSTED_PROXIES must not trust every address, as {text!r} does: any client could "
                "then choose its own address; name the host's proxy addresses only"
            )
        proxies.append(text)
    return tuple(proxies)


def _listen_port(raw: str) -> int | None:
    """$PORT, the port a host routes to; None when it is unset or empty."""
    if not raw:
        return None
    if not re.fullmatch(r"[0-9]{1,5}", raw) or not MIN_LISTEN_PORT <= int(raw) <= MAX_LISTEN_PORT:  # NOSONAR(S6353) ASCII digits: \d also matches others
        raise ValueError(
            f"PORT must be a port number from {MIN_LISTEN_PORT} to {MAX_LISTEN_PORT}, got {raw!r}; set it to the port "
            "the host routes to, or unset it to use --port"
        )
    return int(raw)


def _template_dir(raw: str) -> Path | None:
    """WORKBENCH_PUBLIC_TEMPLATE_DIR, the demo runs the image holds; None when it is unset or empty.

    Absolute only, so the runs copied never depend on the directory the server was started from. What the directory
    holds is checked when the server starts, before the data directory is touched (public_template.plan).
    """
    if not raw:
        return None
    if not raw.isprintable() or not Path(raw).is_absolute():
        raise ValueError(
            "WORKBENCH_PUBLIC_TEMPLATE_DIR must be the absolute path of the demo-run template the image was built with, "
            f"such as /opt/recovery-workbench/demo-runs; got {raw!r}. Leave it empty to seed every run when a viewer "
            "asks"
        )
    return Path(raw)
