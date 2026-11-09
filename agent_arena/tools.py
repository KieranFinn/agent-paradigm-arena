"""Tool abstraction: registration, argument validation, error layering,
side-effect recording.

Error layering (why it matters for the "self-healing" analysis in the blog):
- ``ArgError``: the model passed a bad argument / a patch anchor that does not
  exist.  The model can plausibly fix this itself.
- ``ToolError``: environment limit (file missing, 403, SQL rejected).
  Retrying the same call is useless; the plan/strategy must change.

Both are returned to the model as JSON ``{"ok": false, "kind": ...}`` rather
than raised past the framework, so every paradigm sees the same failure
surface.
"""

from __future__ import annotations

import contextvars
import json
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .llm import Trace


class ArgError(Exception):
    """Model-recoverable: bad arguments, missing patch anchor, bad JSON."""

    kind = "arg"


class ToolError(Exception):
    """Environment limit: file not found, 403, SQL rejected, timeout."""

    kind = "tool"


@dataclass
class ToolSpec:
    name: str
    description: str
    params: Dict[str, str] = field(default_factory=dict)  # name -> "type"
    required: List[str] = field(default_factory=list)
    fn: Callable[..., Any] = None


class _EffectSink:
    def __init__(self) -> None:
        self.items: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    def append(self, item: Dict[str, Any]) -> None:
        with self._lock:
            self.items.append(item)

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(x) for x in self.items]


_sink_var: contextvars.ContextVar[Optional[_EffectSink]] = contextvars.ContextVar(
    "arena_effect_sink", default=None
)


def record_effect(kind: str, **data: Any) -> None:
    """Append an effect for the *current run* (contextvar-scoped)."""
    sink = _sink_var.get()
    if sink is not None:
        sink.append({"kind": kind, **data})


class effects_scope:
    """Context manager capturing all effects recorded inside its body."""

    def __enter__(self) -> "_EffectSink":
        self._sink = _EffectSink()
        self._token = _sink_var.set(self._sink)
        return self._sink

    def __exit__(self, *exc) -> None:
        _sink_var.reset(self._token)


def attach_sink_to_thread(sink: "_EffectSink") -> None:
    """Bind the run's effect sink inside a worker thread (plan executor)."""
    _sink_var.set(sink)


class Toolbox:
    """A per-run set of tools bound to one Trace and one effect sink."""

    def __init__(self, specs: List[ToolSpec], trace: "Trace") -> None:
        self.specs = {s.name: s for s in specs}
        self.trace = trace

    def schemas(self) -> List[Dict[str, Any]]:
        out = []
        for s in self.specs.values():
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": s.name,
                        "description": s.description,
                        "parameters": {
                            "type": "object",
                            "properties": {k: {"type": v} for k, v in s.params.items()},
                            "required": list(s.required),
                        },
                    },
                }
            )
        return out

    def run(self, name: str, args: Dict[str, Any], step: int = 0) -> Dict[str, Any]:
        """Execute one tool call; always returns a JSON-able dict."""
        self.trace.add("tool_call", step=step, payload={"tool": name, "args": _safe(args)}, advance_ms=5.0)
        record_effect("call", tool=name, args=_safe(args), step=step)
        result: Dict[str, Any]
        try:
            spec = self.specs.get(name)
            if spec is None:
                raise ArgError(f"unknown tool: {name!r}; available: {sorted(self.specs)}")
            if not isinstance(args, dict):
                raise ArgError(f"tool arguments must be a JSON object, got: {type(args).__name__}")
            clean = self._validate(spec, args)
            raw = spec.fn(**clean)
            result = raw if isinstance(raw, dict) else {"ok": True, "result": raw}
        except ArgError as e:
            result = {"ok": False, "kind": "arg", "error": str(e)}
        except ToolError as e:
            result = {"ok": False, "kind": "tool", "error": str(e)}
        except TypeError as e:
            result = {"ok": False, "kind": "arg", "error": f"bad arguments: {e}"}
        except Exception as e:  # noqa: BLE001 - implementation defect surfaced as tool error
            result = {"ok": False, "kind": "tool", "error": f"{type(e).__name__}: {e}"}
        record_effect("result", tool=name, ok=bool(result.get("ok")), summary=_result_summary(result))
        self.trace.add("tool_result", step=step, payload={"tool": name, "result": result}, advance_ms=30.0)
        return result

    @staticmethod
    def _validate(spec: ToolSpec, args: Dict[str, Any]) -> Dict[str, Any]:
        clean: Dict[str, Any] = {}
        for key, value in args.items():
            if key not in spec.params:
                raise ArgError(
                    f"unknown parameter {key!r} for tool {spec.name!r}; "
                    f"known: {sorted(spec.params)}"
                )
            expected = spec.params[key]
            if expected == "string" and not isinstance(value, str):
                raise ArgError(f"parameter {key!r} must be a string")
            if expected == "integer" and not (isinstance(value, int) and not isinstance(value, bool)):
                raise ArgError(f"parameter {key!r} must be an integer")
            if expected == "number" and not (isinstance(value, (int, float)) and not isinstance(value, bool)):
                raise ArgError(f"parameter {key!r} must be a number")
            if expected == "boolean" and not isinstance(value, bool):
                raise ArgError(f"parameter {key!r} must be a boolean")
            clean[key] = value
        for req in spec.required:
            if req not in clean:
                raise ArgError(f"missing required parameter {req!r} for tool {spec.name!r}")
        return clean


def _safe(obj: Any) -> Any:
    """Make an object JSON-able for traces (truncate long strings)."""
    try:
        json.dumps(obj)
        if isinstance(obj, dict):
            return {k: _safe(v) for k, v in obj.items()}
        return obj
    except (TypeError, ValueError):
        return repr(obj)[:2000]


def _result_summary(result: Dict[str, Any]) -> str:
    if result.get("ok"):
        return str(result.get("summary", "ok"))[:400]
    return str(result.get("error", "error"))[:400]
