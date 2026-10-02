"""The only place model calls happen. Every call is logged to `llm_calls` with tokens, cost and latency."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from string import Template
from typing import TYPE_CHECKING, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy import Engine

from jobhunt.db import queries

if TYPE_CHECKING:
    from jobhunt.config import AppConfig
    from jobhunt.config import LocalLLM as LocalLLMSettings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# USD per million tokens (input, output). Longest-prefix match on the model id.
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-fable-5": (10.00, 50.00),
}


def price_for(model: str) -> tuple[float, float]:
    best = ""
    for prefix in PRICES:
        if model.startswith(prefix) and len(prefix) > len(best):
            best = prefix
    return PRICES.get(best, (0.0, 0.0))


def cost_usd(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float:
    """`input_tokens` is every input token; of those, `cache_read` are billed at 0.1x and `cache_write` at 1.25x (the
    5-minute prompt cache)."""
    pin, pout = price_for(model)
    plain = max(0, input_tokens - cache_read - cache_write)
    return round((plain * pin + cache_read * pin * 0.1 + cache_write * pin * 1.25 + output_tokens * pout) / 1_000_000,
                 6)


def load_prompt(name: str, version: str) -> Template:
    path = PROMPTS_DIR / f"{name}.{version}.md"
    with open(path, encoding="utf-8") as fh:
        return Template(fh.read())


class MissingAPIKey(RuntimeError):
    pass


class LLMOutputError(RuntimeError):
    pass


class LLMUnavailable(RuntimeError):
    """The configured model cannot be reached or is not installed."""


class LLMClient(Protocol):
    def structured(self, schema: type[T], *, system: str, user: str, purpose: str, model: str,
                   prompt_version: str, job_id: int | None = None, max_tokens: int = 4096,
                   cache_system: bool = False) -> T: ...


class AnthropicLLM:
    """langchain-anthropic ChatAnthropic with Claude's native JSON-schema structured output."""

    def __init__(self, api_key: str, engine: Engine, effort_for: Callable[[str], str | None] | None = None) -> None:
        if not api_key:
            raise MissingAPIKey("ANTHROPIC_API_KEY is not set (put it in .env)")
        self.api_key = api_key
        self.engine = engine
        self.effort_for = effort_for or (lambda purpose: None)
        self._models: dict[tuple[str, int, str | None], object] = {}
        self._lock = threading.Lock()

    def _chat(self, model: str, max_tokens: int, effort: str | None = None):  # noqa: ANN202
        from langchain_anthropic import ChatAnthropic

        key = (model, max_tokens, effort)
        with self._lock:
            if key not in self._models:
                # Long outputs (and the model's reasoning, which shares the output budget) need a longer timeout.
                kwargs: dict = {"model": model, "api_key": self.api_key, "max_tokens": max_tokens,
                                "timeout": max(180, max_tokens // 25), "max_retries": 3}
                if max_tokens > 16000:
                    kwargs["streaming"] = True  # the API requires streaming for long outputs; the reply is assembled
                if model.startswith("claude-haiku"):
                    kwargs["temperature"] = 0  # reproducible scoring; newer models reject sampling params
                elif effort:
                    kwargs["effort"] = effort  # output_config.effort: bounds reasoning on Sonnet/Opus 5
                self._models[key] = ChatAnthropic(**kwargs)
            return self._models[key]

    def structured(self, schema: type[T], *, system: str, user: str, purpose: str, model: str,
                   prompt_version: str, job_id: int | None = None, max_tokens: int = 4096,
                   cache_system: bool = False) -> T:
        from langchain_core.messages import HumanMessage, SystemMessage

        chat = self._chat(model, max_tokens, self.effort_for(purpose))
        runnable = chat.with_structured_output(schema, method="json_schema", include_raw=True)
        # cache_system: the system prompt is the same for many calls in a row (scoring: your background and the
        # rubric), so it is cached; each call then pays 0.1x for it instead of 1x
        system_content = ([{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
                          if cache_system else system)
        started = time.perf_counter()
        out = runnable.invoke([SystemMessage(content=system_content), HumanMessage(content=user)])
        latency_ms = int((time.perf_counter() - started) * 1000)
        raw = out.get("raw")
        usage = getattr(raw, "usage_metadata", None) or {}
        in_tok, out_tok = int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        details = usage.get("input_token_details") or {}
        # langchain-anthropic reports cache writes per TTL (ephemeral_5m / ephemeral_1h), not always as cache_creation
        read = int(details.get("cache_read") or 0)
        write = int(details.get("cache_creation") or 0) or (int(details.get("ephemeral_5m_input_tokens") or 0)
                                                            + int(details.get("ephemeral_1h_input_tokens") or 0))
        if cache_system:
            log.info("%s: %d input tokens, %d from cache, %d written to cache", purpose, in_tok, read, write)
        queries.insert_llm_call(self.engine, job_id=job_id, purpose=purpose, model=model,
                                prompt_version=prompt_version, input_tokens=in_tok, output_tokens=out_tok,
                                cost_usd=cost_usd(model, in_tok, out_tok, read, write), latency_ms=latency_ms)
        parsed = out.get("parsed")
        stop = (getattr(raw, "response_metadata", None) or {}).get("stop_reason")
        if stop == "max_tokens" and (out.get("parsing_error") or parsed is None):
            raise LLMOutputError(f"{purpose}: the reply hit the {max_tokens}-token output limit before it was complete "
                                 f"({out_tok} tokens used). Retry; if it happens again, the limit for this step "
                                 "needs raising in tailor/drafting.py.")
        if out.get("parsing_error") or parsed is None:
            raise LLMOutputError(f"{purpose}: structured output could not be parsed: {out.get('parsing_error')}")
        return parsed  # type: ignore[return-value]


# --------------------------------------------------------------------------------------------- local models

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)


def _extract_json(text: str) -> str:
    """Strip reasoning blocks and code fences; return the outermost JSON object."""
    text = _THINK_BLOCK.sub("", text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    return text[start: end + 1] if start != -1 and end > start else text


def _model_name(name: str) -> str:
    return name if ":" in name else f"{name}:latest"


class LocalLLM:
    """Structured output from a locally served model (default: Qwen3-8B in Ollama). Cost is logged as $0."""

    def __init__(self, engine: Engine, settings: Callable[[], LocalLLMSettings],
                 client: httpx.Client | None = None) -> None:
        self.engine = engine
        self.settings = settings
        self._client = client

    def _http(self, timeout: float) -> httpx.Client:
        return self._client or httpx.Client(timeout=timeout)

    # -- health ---------------------------------------------------------------------------------------------
    def check(self) -> dict:
        """Reachability and installed models. Returns {ok, message, version, models}."""
        s = self.settings()
        client = self._http(10)
        try:
            if s.api == "ollama":
                version = client.get(f"{s.base_url}/api/version").json().get("version")
                models = [m.get("name") or m.get("model") for m in client.get(f"{s.base_url}/api/tags").json()
                          .get("models", [])]
                present = _model_name(s.model) in {_model_name(m) for m in models if m}
                message = (f"Ollama {version} is running; {s.model} is installed" if present else
                           f"Ollama {version} is running but {s.model} is not installed — run: ollama pull {s.model}")
                return {"ok": present, "message": message, "version": version, "models": models}
            data = client.get(f"{s.base_url}/v1/models").json()
            models = [m.get("id") for m in data.get("data", [])]
            present = s.model in models or not models
            return {"ok": present, "version": None, "models": models,
                    "message": f"server is running; models: {', '.join(models) or 'n/a'}" if present else
                    f"server is running but does not list {s.model}; available: {', '.join(models)}"}
        except (httpx.HTTPError, ValueError) as exc:
            hint = " — is Ollama running? (start the Ollama app, or run `ollama serve`)" if s.api == "ollama" else ""
            return {"ok": False, "message": f"cannot reach {s.base_url}: {exc}{hint}", "version": None, "models": []}
        finally:
            if self._client is None:
                client.close()

    # -- calls ----------------------------------------------------------------------------------------------
    def structured(self, schema: type[T], *, system: str, user: str, purpose: str, model: str,
                   prompt_version: str, job_id: int | None = None, max_tokens: int = 4096,
                   cache_system: bool = False) -> T:
        s = self.settings()
        json_schema = schema.model_json_schema()
        # Qwen3 soft switch; harmless for other models. The schema is also stated in the prompt, which helps small
        # models even when the server enforces it with a grammar.
        user_text = user + ("\n/no_think" if not s.think else "")
        system_text = (system + "\n\nRespond with only a JSON object matching this JSON schema:\n"
                       + json.dumps(json_schema, ensure_ascii=False))
        messages = [{"role": "system", "content": system_text}, {"role": "user", "content": user_text}]
        client = self._http(s.timeout_s)
        started = time.perf_counter()
        try:
            if s.api == "ollama":
                body = {"model": s.model, "messages": messages, "stream": False, "format": json_schema,
                        "think": s.think, "keep_alive": s.keep_alive,
                        "options": {"num_ctx": s.num_ctx, "temperature": s.temperature,
                                    "num_predict": min(max_tokens, s.num_ctx)}}
                r = client.post(f"{s.base_url}/api/chat", json=body, timeout=s.timeout_s)
                if r.status_code == 404:
                    raise LLMUnavailable(f"{s.model} is not installed in Ollama — run: ollama pull {s.model}")
                r.raise_for_status()
                data = r.json()
                content = (data.get("message") or {}).get("content", "")
                in_tok, out_tok = int(data.get("prompt_eval_count") or 0), int(data.get("eval_count") or 0)
                if in_tok and in_tok >= s.num_ctx - 8:
                    raise LLMOutputError(
                        f"{purpose}: the prompt ({in_tok} tokens) filled the model's context window; Ollama "
                        f"truncates silently. Raise local_llm.num_ctx above {s.num_ctx} (Settings → Language models).")
            else:
                body = {"model": s.model, "messages": messages, "temperature": s.temperature, "max_tokens": max_tokens,
                        "response_format": {"type": "json_schema",
                                            "json_schema": {"name": schema.__name__, "schema": json_schema}}}
                r = client.post(f"{s.base_url}/v1/chat/completions", json=body, timeout=s.timeout_s)
                r.raise_for_status()
                data = r.json()
                content = data["choices"][0]["message"].get("content") or ""
                usage = data.get("usage") or {}
                in_tok, out_tok = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        except httpx.ConnectError as exc:
            raise LLMUnavailable(f"cannot reach the local model server at {s.base_url} ({exc}) — "
                                 "is Ollama running?") from exc
        except httpx.TimeoutException as exc:
            raise LLMUnavailable(f"the local model did not answer within {s.timeout_s:.0f}s "
                                 "(raise local_llm.timeout_s or lower concurrency)") from exc
        except httpx.HTTPStatusError as exc:
            raise LLMUnavailable(f"local model server error {exc.response.status_code}: "
                                 f"{exc.response.text[:300]}") from exc
        finally:
            if self._client is None:
                client.close()
        latency_ms = int((time.perf_counter() - started) * 1000)
        queries.insert_llm_call(self.engine, job_id=job_id, purpose=purpose, model=f"local:{s.model}",
                                prompt_version=prompt_version, input_tokens=in_tok, output_tokens=out_tok,
                                cost_usd=0.0, latency_ms=latency_ms)
        try:
            return schema.model_validate_json(_extract_json(content))
        except ValidationError as exc:
            raise LLMOutputError(f"{purpose}: local model output did not match the schema: "
                                 f"{str(exc)[:400]}") from exc


# --------------------------------------------------------------------------------------------- routing

TASK_FOR_PURPOSE = {"score": "scoring", "requirements": "tailoring", "draft_resume": "tailoring",
                    "draft_cover": "tailoring"}


class RoutingLLM:
    """Sends each call to Claude or the local model according to scoring.provider / tailoring.provider, read from
    the live config on every call (so a change on the Settings page applies immediately)."""

    def __init__(self, engine: Engine, config: Callable[[], AppConfig], *, local_client: httpx.Client | None = None,
                 override: str | None = None, anthropic: LLMClient | None = None) -> None:
        self.engine = engine
        self.config = config
        self.local_client = local_client
        self.override = override
        self._anthropic = anthropic
        self._lock = threading.Lock()
        self.local = LocalLLM(engine, lambda: self.config().local_llm, local_client)

    def _effort_for(self, purpose: str) -> str | None:
        if TASK_FOR_PURPOSE.get(purpose) == "tailoring":
            return self.config().tailoring.effort or None
        return None

    def provider_for(self, purpose: str) -> str:
        if self.override:
            return self.override
        cfg = self.config()
        return getattr(cfg, TASK_FOR_PURPOSE.get(purpose, "scoring")).provider

    def model_label(self, purpose: str) -> str:
        cfg = self.config()
        if self.provider_for(purpose) == "local":
            return f"local:{cfg.local_llm.model}"
        return cfg.scoring.model if TASK_FOR_PURPOSE.get(purpose, "scoring") == "scoring" else cfg.tailoring.model

    def concurrency(self, purpose: str) -> int:
        cfg = self.config()
        return cfg.local_llm.concurrency if self.provider_for(purpose) == "local" else cfg.scoring.concurrency

    def anthropic(self) -> LLMClient:
        with self._lock:
            if self._anthropic is None:
                self._anthropic = AnthropicLLM(self.config().secrets.ANTHROPIC_API_KEY or "", self.engine,
                                               effort_for=self._effort_for)
            return self._anthropic

    def with_provider(self, provider: str | None) -> RoutingLLM:
        if not provider:
            return self
        return RoutingLLM(self.engine, self.config, local_client=self.local_client, override=provider,
                          anthropic=self._anthropic)

    def preflight(self, purpose: str) -> None:
        """Fail fast (once, before a batch) instead of logging the same error for every job."""
        if self.provider_for(purpose) == "local":
            status = self.local.check()
            if not status["ok"]:
                raise LLMUnavailable(status["message"])
        else:
            self.anthropic()  # raises MissingAPIKey when no key is configured

    def structured(self, schema: type[T], *, system: str, user: str, purpose: str, model: str,
                   prompt_version: str, job_id: int | None = None, max_tokens: int = 4096,
                   cache_system: bool = False) -> T:
        if self.provider_for(purpose) == "local":
            return self.local.structured(schema, system=system, user=user, purpose=purpose, model=model,
                                         prompt_version=prompt_version, job_id=job_id, max_tokens=max_tokens)
        return self.anthropic().structured(schema, system=system, user=user, purpose=purpose, model=model,
                                           prompt_version=prompt_version, job_id=job_id, max_tokens=max_tokens,
                                           cache_system=cache_system)


class ConnectionTest(BaseModel):
    ok: bool
    sentence: str


def check_local_model(llm: LocalLLM) -> dict:
    """Health check plus one tiny structured call. Returns {ok, message, seconds, tokens_per_second}."""
    status = llm.check()
    if not status["ok"]:
        return {**status, "seconds": None}
    started = time.perf_counter()
    try:
        out = llm.structured(ConnectionTest, system="You are a connectivity test.",
                             user="Set ok to true and write one short sentence confirming you can produce JSON.",
                             purpose="llm_test", model="", prompt_version="llm_test", max_tokens=200)
    except (LLMUnavailable, LLMOutputError) as exc:
        return {"ok": False, "message": str(exc), "seconds": round(time.perf_counter() - started, 1)}
    seconds = round(time.perf_counter() - started, 1)
    return {"ok": bool(out.ok), "seconds": seconds,
            "message": f"{status['message']}. Structured reply in {seconds}s (includes loading the model the first "
                       f"time): {out.sentence!r}"}


_LLM: LLMClient | None = None


def get_llm(api_key: str | None, engine: Engine) -> LLMClient:
    global _LLM
    if _LLM is None:
        _LLM = AnthropicLLM(api_key or "", engine)
    return _LLM


def set_llm(llm: LLMClient | None) -> None:
    global _LLM
    _LLM = llm
