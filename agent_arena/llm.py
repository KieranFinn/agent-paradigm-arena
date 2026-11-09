"""Transport layer + Trace + simulated clock + pricing + audit cache.

- Dual transport: uses the ``openai`` SDK when installed, else stdlib
  ``urllib``.  Both speak the OpenAI chat-completions protocol, so any
  compatible endpoint (DeepSeek, GLM, ...) works via config.
- ``Trace`` records one JSONL event per step; its aggregation methods are
  exactly the columns of the blog's comparison tables.
- Latency is a *simulated* clock (see docs/design.md rule 3) so network
  jitter never contaminates the comparison; real wall-clock is kept in
  ``raw.jsonl`` at the run level, outside the report's main tables.
- Pricing is tiered: cached input / fresh input / output, CNY per 1M tokens.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------
# Trace
# --------------------------------------------------------------------------

EVENT_KINDS = (
    "llm_call",
    "tool_call",
    "tool_result",
    "plan",
    "eval",
    "reflection",
    "guard",
    "final",
)


@dataclass
class TraceEvent:
    step: int
    kind: str
    t_ms: float
    latency_ms: float
    model: str
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    cost_cny: float
    payload: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "step": self.step,
            "kind": self.kind,
            "t_ms": round(self.t_ms, 1),
            "latency_ms": round(self.latency_ms, 1),
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "cost_cny": round(self.cost_cny, 6),
            "payload": self.payload,
        }


class BudgetExceeded(Exception):
    """Raised when a single run exceeds budget.max_cny_per_run."""


class Trace:
    """Event sequence of one run + the simulated clock + aggregations."""

    def __init__(self) -> None:
        self.events: List[TraceEvent] = []
        self._t = 0.0
        self._lock = threading.Lock()

    # -- recording ---------------------------------------------------------
    def add(
        self,
        kind: str,
        step: int = 0,
        payload: Optional[Dict[str, Any]] = None,
        latency_ms: float = 0.0,
        model: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cached_tokens: int = 0,
        cost_cny: float = 0.0,
        advance_ms: float = 0.0,
    ) -> TraceEvent:
        assert kind in EVENT_KINDS, kind
        with self._lock:
            ev = TraceEvent(
                step=step,
                kind=kind,
                t_ms=self._t,
                latency_ms=latency_ms,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cached_tokens=cached_tokens,
                cost_cny=cost_cny,
                payload=payload or {},
            )
            self.events.append(ev)
            self._t += advance_ms
            return ev

    # -- aggregations (one per blog table column) ---------------------------
    def to_first_action_ms(self) -> Optional[float]:
        for e in self.events:
            if e.kind in ("tool_call", "plan"):
                return round(e.t_ms, 1)
        return None

    def total_tokens(self) -> Dict[str, int]:
        p = sum(e.prompt_tokens for e in self.events if e.kind == "llm_call")
        c = sum(e.completion_tokens for e in self.events if e.kind == "llm_call")
        k = sum(e.cached_tokens for e in self.events if e.kind == "llm_call")
        return {"prompt": p, "completion": c, "cached": k}

    def peak_prompt_tokens(self) -> int:
        return max((e.prompt_tokens for e in self.events if e.kind == "llm_call"), default=0)

    def total_cost_cny(self) -> float:
        return round(sum(e.cost_cny for e in self.events if e.kind == "llm_call"), 6)

    def total_latency_ms(self) -> float:
        return round(self._t, 1)

    def llm_calls(self) -> int:
        return sum(1 for e in self.events if e.kind == "llm_call")

    def count(self, kind: str) -> int:
        return sum(1 for e in self.events if e.kind == kind)

    def as_dicts(self) -> List[Dict[str, Any]]:
        return [e.as_dict() for e in self.events]


# --------------------------------------------------------------------------
# Pricing / simulated latency
# --------------------------------------------------------------------------


@dataclass
class Price:
    input_cached: float = 0.2  # CNY per 1M cached prompt tokens
    input: float = 2.0  # CNY per 1M fresh prompt tokens
    output: float = 8.0  # CNY per 1M completion tokens

    def cost(self, prompt: int, completion: int, cached: int) -> float:
        cached = min(cached, prompt)
        fresh = prompt - cached
        return (cached / 1e6 * self.input_cached
                + fresh / 1e6 * self.input
                + completion / 1e6 * self.output)


def simulated_latency_ms(prompt_tokens: int, completion_tokens: int, cached_tokens: int) -> float:
    """Deterministic serving-time model: cached -> 40ms, else TTFT+decode."""
    if prompt_tokens and cached_tokens >= prompt_tokens:
        return 40.0
    fresh = max(prompt_tokens - cached_tokens, 0)
    return round(450.0 + fresh * 0.02 + completion_tokens * 12.0, 1)


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------


@dataclass
class ToolCall:
    id: str
    name: str
    args: Dict[str, Any]


@dataclass
class Msg:
    """Normalized assistant message."""

    content: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"

    def to_api(self) -> Dict[str, Any]:
        m: Dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            m["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.args, ensure_ascii=False),
                    },
                }
                for tc in self.tool_calls
            ]
        return m


def parse_json_loose(text: str) -> Any:
    """Extract the first balanced JSON object/array from LLM output."""
    if text is None:
        raise ValueError("empty response")
    s = text.strip()
    # strip markdown fences
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
        s = s.strip()
    # try direct
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        pass
    # find first balanced {...} or [...]
    for opener, closer in (("{", "}"), ("[", "]")):
        start = s.find(opener)
        if start == -1:
            continue
        depth, in_str, esc = 0, False, False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    candidate = s[start : i + 1]
                    try:
                        return json.loads(candidate)
                    except (json.JSONDecodeError, ValueError):
                        break
        # fall through to next opener
    raise ValueError(f"no parseable JSON in response: {text[:200]!r}")


# --------------------------------------------------------------------------
# Audit cache
# --------------------------------------------------------------------------


class AuditCache:
    """Request-content-keyed response cache (opt-in via --use-cache).

    NOTE (design deviation, see docs/design.md): the cache is OFF by default
    inside a batch.  With temperature=0 and deterministic mock tools, cache
    hits within one batch would make the N repeats of a cell byte-identical
    and degenerate the Wilson intervals.  It exists for development
    iterations and for resuming interrupted batches.
    """

    def __init__(self, path: str, enabled: bool) -> None:
        self.path = Path(path)
        self.enabled = enabled
        self._mem: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        if enabled and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    self._mem[rec["key"]] = rec["response"]
                except (json.JSONDecodeError, KeyError):
                    continue

    @staticmethod
    def key(model: str, messages: List[Dict[str, Any]], tools: Optional[List[Any]],
            temperature: float, max_tokens: Optional[int]) -> str:
        blob = json.dumps(
            {"model": model, "messages": messages, "tools": tools,
             "temperature": temperature, "max_tokens": max_tokens},
            sort_keys=True, ensure_ascii=False, default=str,
        )
        return hashlib.sha256(blob.encode()).hexdigest()

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        with self._lock:
            return self._mem.get(key)

    def put(self, key: str, response: Dict[str, Any]) -> None:
        if not self.enabled:
            return
        with self._lock:
            if key in self._mem:
                return
            self._mem[key] = response
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"key": key, "response": response}, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------
# Transports
# --------------------------------------------------------------------------


class LLMError(Exception):
    pass


class _UrllibTransport:
    def request(self, base_url: str, api_key: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        url = base_url.rstrip("/") + "/chat/completions"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:2000]
            raise _HttpError(e.code, body) from None
        except urllib.error.URLError as e:
            raise _HttpError(0, f"network error: {e.reason}") from None


class _HttpError(Exception):
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body[:300]}")


try:  # optional SDK
    from openai import OpenAI as _OpenAI  # type: ignore

    class _SDKTransport:
        def __init__(self) -> None:
            self._clients: Dict[str, Any] = {}
            self._lock = threading.Lock()

        def request(self, base_url: str, api_key: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
            with self._lock:
                client = self._clients.get((base_url, api_key))
                if client is None:
                    client = _OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
                    self._clients[(base_url, api_key)] = client
            kwargs = dict(payload)
            kwargs["timeout"] = timeout
            try:
                resp = client.chat.completions.create(**kwargs)
            except Exception as e:  # noqa: BLE001 - normalize SDK errors
                status = getattr(e, "status_code", 0) or 0
                raise _HttpError(int(status), str(e)) from None
            return resp.model_dump()

    _TRANSPORT = _SDKTransport()
except ImportError:  # pragma: no cover - stdlib fallback
    _TRANSPORT = _UrllibTransport()


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

_RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504}


def load_env_file(path: str | Path = ".env") -> Dict[str, str]:
    """Minimal .env loader (KEY=VALUE lines). Does not override real env."""
    loaded: Dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return loaded
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value
        loaded[key] = value
    return loaded


class LLMClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
        temperature: float = 0,
        max_tokens: int = 4096,
        price: Optional[Price] = None,
        trace: Optional[Trace] = None,
        cache: Optional[AuditCache] = None,
        budget_cny: float = 500.0,
        request_timeout_s: float = 180.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.price = price or Price()
        self.trace = trace if trace is not None else Trace()
        self.cache = cache or AuditCache("/dev/null", enabled=False)
        self.budget_cny = budget_cny
        self.request_timeout_s = request_timeout_s
        self.total_real_ms = 0.0  # wall-clock accumulator (raw.jsonl only)

    # -- public API ---------------------------------------------------------
    def chat(
        self,
        messages: List[Any],
        tools: Optional[List[Dict[str, Any]]] = None,
        step: int = 0,
        label: str = "",
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> Msg:
        model = model or self.model
        api_messages = [m.to_api() if isinstance(m, Msg) else m for m in messages]
        payload: Dict[str, Any] = {
            "model": model,
            "messages": api_messages,
            "temperature": self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
        }
        if tools:
            payload["tools"] = tools

        key = AuditCache.key(model, api_messages, tools, self.temperature, payload["max_tokens"])
        cached_resp = self.cache.get(key)
        t0 = time.monotonic()
        if cached_resp is not None:
            content, raw_tools, usage, finish = (
                cached_resp["content"],
                cached_resp["tool_calls"],
                cached_resp["usage"],
                cached_resp.get("finish_reason", "stop"),
            )
            from_cache = True
        else:
            data = self._request_with_retry(payload)
            self.total_real_ms += (time.monotonic() - t0) * 1000
            choice = data["choices"][0]
            message = choice.get("message", {})
            content = message.get("content") or ""
            raw_tools = message.get("tool_calls") or []
            usage = data.get("usage") or {}
            finish = choice.get("finish_reason", "stop")
            self.cache.put(key, {"content": content, "tool_calls": raw_tools,
                                 "usage": usage, "finish_reason": finish})
            from_cache = False

        tool_calls = []
        for tc in raw_tools:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"__raw_arguments__": (fn.get("arguments") or "")[:500]}
            tool_calls.append(ToolCall(id=tc.get("id") or "", name=fn.get("name") or "", args=args))

        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        cached_tokens = int(
            usage.get("prompt_cache_hit_tokens")
            or ((usage.get("prompt_tokens_details") or {}).get("cached_tokens"))
            or 0
        )
        cost = self.price.cost(prompt_tokens, completion_tokens, cached_tokens)
        lat = 40.0 if from_cache else simulated_latency_ms(prompt_tokens, completion_tokens, cached_tokens)

        self.trace.add(
            "llm_call",
            step=step,
            payload={
                "label": label,
                "from_cache": from_cache,
                "finish_reason": finish,
                "n_messages": len(api_messages),
                "prompt_chars": sum(len(str(m.get("content") or "")) for m in api_messages),
                "content": content,
                "tool_calls": [{"name": tc.name, "args": tc.args} for tc in tool_calls],
            },
            latency_ms=lat,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
            cost_cny=cost,
            advance_ms=lat,
        )
        if self.trace.total_cost_cny() > self.budget_cny:
            raise BudgetExceeded(
                f"run budget exceeded: {self.trace.total_cost_cny():.2f} CNY > {self.budget_cny:.2f} CNY"
            )
        return Msg(content=content, tool_calls=tool_calls, finish_reason=finish)

    def text(
        self,
        system: str,
        user: str,
        step: int = 0,
        label: str = "",
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        msg = self.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            tools=None,
            step=step,
            label=label,
            model=model,
            max_tokens=max_tokens,
        )
        return msg.content

    def json_call(
        self,
        system: str,
        user: str,
        step: int = 0,
        label: str = "",
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> Any:
        """Ask for strict JSON; one repair round on parse failure."""
        messages: List[Any] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        msg = self.chat(messages, tools=None, step=step, label=label, model=model, max_tokens=max_tokens)
        try:
            return parse_json_loose(msg.content)
        except ValueError as e:
            messages = messages + [
                msg,
                {"role": "user", "content": f"Your reply was not valid JSON ({e}). Reply again with ONLY the valid JSON, no prose, no code fences."},
            ]
            msg = self.chat(messages, tools=None, step=step, label=label + ".repair", model=model, max_tokens=max_tokens)
            return parse_json_loose(msg.content)  # raises on second failure

    # -- internals ------------------------------------------------------------
    def _request_with_retry(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        last: Optional[Exception] = None
        for attempt in range(5):
            try:
                data = _TRANSPORT.request(self.base_url, self.api_key or "", payload, self.request_timeout_s)
                if "choices" not in data:
                    raise LLMError(f"unexpected API response shape: {str(data)[:300]}")
                return data
            except _HttpError as e:
                last = e
                if e.status in _RETRYABLE or e.status == 0:
                    sleep = min(2 ** attempt + random.random(), 30.0)
                    time.sleep(sleep)
                    continue
                raise LLMError(str(e)) from None
            except LLMError:
                raise
        raise LLMError(f"retries exhausted: {last}")
