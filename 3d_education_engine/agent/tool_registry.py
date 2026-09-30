"""The only door from language to action.

The LLM (and the rule-based parser) can ask for tools by NAME with JSON
arguments. Every call is checked here before anything runs:

  1. the tool exists;
  2. the arguments match its JSON schema (types, ranges, enums, required,
     no unknown keys);
  3. semantic checks pass (the model id is registered, the value is sane);

and only then is the handler run -- on the UI thread, through `executor`,
because VTK must only be touched from one thread. There is no tool that
evaluates code, imports modules or opens arbitrary files.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)

Executor = Callable[[Callable[[], Any]], Any]


class ToolError(ValueError):
    """Bad arguments or an impossible request. The message is shown to the LLM/user."""


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    id: str = ""


@dataclass
class ToolResult:
    call: ToolCall
    ok: bool
    content: dict[str, Any]

    @property
    def message(self) -> str:
        return str(self.content.get("message") or self.content.get("error") or "")

    def to_json(self) -> str:
        return json.dumps({"ok": self.ok, **self.content}, ensure_ascii=False, default=str)[:4000]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict              # JSON schema (object)
    handler: Callable[..., dict]
    check: Callable[[dict], None] | None = None
    category: str = "visualization"
    # False for tools that only read the database or the network: they run on
    # the agent's thread so a slow lookup never freezes the screen.
    main_thread: bool = True

    def openai(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                  "parameters": self.parameters}}


def _type_ok(value: Any, kind: str) -> bool:
    return {
        "string": lambda v: isinstance(v, str),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "array": lambda v: isinstance(v, list),
        "object": lambda v: isinstance(v, dict),
        "null": lambda v: v is None,
    }[kind](value)


def validate(schema: dict, args: dict, path: str = "") -> list[str]:
    """A strict subset of JSON Schema: enough for tool arguments, nothing more."""
    errors: list[str] = []
    if not isinstance(args, dict):
        return [f"{path or 'arguments'}: expected an object"]
    props = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in args:
            errors.append(f"{path}{key}: required")
    if schema.get("additionalProperties", False) is False:
        for key in args:
            if key not in props:
                errors.append(f"{path}{key}: unknown argument (allowed: {', '.join(props) or 'none'})")
    for key, value in args.items():
        spec = props.get(key)
        if spec is None:
            continue
        kinds = spec.get("type", "string")
        kinds = kinds if isinstance(kinds, list) else [kinds]
        if not any(_type_ok(value, k) for k in kinds):
            errors.append(f"{path}{key}: expected {' or '.join(kinds)}")
            continue
        if "enum" in spec and value not in spec["enum"]:
            errors.append(f"{path}{key}: must be one of {spec['enum']}")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in spec and value < spec["minimum"]:
                errors.append(f"{path}{key}: must be ≥ {spec['minimum']}")
            if "maximum" in spec and value > spec["maximum"]:
                errors.append(f"{path}{key}: must be ≤ {spec['maximum']}")
        if isinstance(value, str) and len(value) > spec.get("maxLength", 200):
            errors.append(f"{path}{key}: too long")
        if isinstance(value, list):
            if "minItems" in spec and len(value) < spec["minItems"]:
                errors.append(f"{path}{key}: needs at least {spec['minItems']} items")
            if "maxItems" in spec and len(value) > spec["maxItems"]:
                errors.append(f"{path}{key}: at most {spec['maxItems']} items")
            item = spec.get("items")
            if item:
                for i, v in enumerate(value):
                    if not _type_ok(v, item.get("type", "string")):
                        errors.append(f"{path}{key}[{i}]: expected {item.get('type')}")
    return errors


class ToolRegistry:
    def __init__(self, executor: Executor | None = None):
        self.tools: dict[str, Tool] = {}
        self.executor: Executor = executor or (lambda fn: fn())
        self.log: list[ToolResult] = []

    def register(self, tool: Tool) -> None:
        if tool.name in self.tools:
            raise ValueError(f"tool {tool.name} registered twice")
        if tool.parameters.get("type") != "object":
            raise ValueError(f"tool {tool.name}: parameters must be an object schema")
        self.tools[tool.name] = tool

    def names(self) -> list[str]:
        return sorted(self.tools)

    def openai_tools(self, categories: set[str] | None = None) -> list[dict]:
        return [t.openai() for t in self.tools.values() if categories is None or t.category in categories]

    def execute(self, call: ToolCall) -> ToolResult:
        tool = self.tools.get(call.name)
        if tool is None:
            return self._record(call, False, {"error": f"there is no tool called {call.name!r}"})
        args = dict(call.arguments or {})
        problems = validate(tool.parameters, args)
        if problems:
            return self._record(call, False, {"error": "invalid arguments: " + "; ".join(problems)})
        try:
            if tool.check is not None:
                tool.check(args)
            run = lambda: tool.handler(**args)
            content = self.executor(run) if tool.main_thread else run()
            return self._record(call, True, content if isinstance(content, dict) else {"result": content})
        except (ToolError, ValueError, KeyError, LookupError) as exc:
            msg = exc.args[0] if exc.args else str(exc)
            return self._record(call, False, {"error": str(msg)})
        except Exception as exc:                  # a bug, not a bad request: log it fully
            log.exception("tool %s failed", call.name)
            return self._record(call, False, {"error": f"internal error in {call.name}: {exc}"})

    def _record(self, call: ToolCall, ok: bool, content: dict) -> ToolResult:
        result = ToolResult(call, ok, content)
        self.log.append(result)
        del self.log[:-200]
        (log.info if ok else log.warning)("tool %s(%s) -> %s", call.name, call.arguments, result.message[:160])
        return result


# ---------------------------------------------------------------------- schema helpers
def obj(properties: dict | None = None, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties or {}, "required": required or [],
            "additionalProperties": False}


def string(description: str, enum: list[str] | None = None) -> dict:
    d = {"type": "string", "description": description}
    if enum:
        d["enum"] = enum
    return d


def number(description: str, minimum: float | None = None, maximum: float | None = None) -> dict:
    d: dict = {"type": "number", "description": description}
    if minimum is not None:
        d["minimum"] = minimum
    if maximum is not None:
        d["maximum"] = maximum
    return d


def vec3(description: str) -> dict:
    return {"type": "array", "description": description, "items": {"type": "number"}, "minItems": 3, "maxItems": 3}
