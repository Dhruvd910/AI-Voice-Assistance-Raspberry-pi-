"""The agent: speech/text in, validated tool calls out, a short spoken reply back.

    text -> IntentParser (offline, instant) -> tools
         -> questions: tools (highlight) + RAG passages -> LLM (or the passages alone, offline)
         -> anything else: LLM with tool calling -> tools
         -> no LLM available: resolver / RAG fallback

The LLM never executes anything itself. It proposes tool calls; the
ToolRegistry validates and runs them.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from agent.intent_parser import IntentParser, ParseResult, build_context
from agent.llm_client import LLMClient, LLMError
from agent.tool_registry import ToolCall, ToolResult
from knowledge.rag import offline_answer

if TYPE_CHECKING:
    from app.dependency_container import Services

log = logging.getLogger(__name__)
PROMPT = Path(__file__).with_name("prompts") / "system.md"


@dataclass
class AgentReply:
    text: str
    results: list[ToolResult] = field(default_factory=list)
    used_llm: bool = False

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)


class Agent:
    def __init__(self, services: "Services"):
        self.sv = services
        self.parser = IntentParser(services.resolver)
        self.llm = LLMClient(services.settings.llm)

    @property
    def llm_available(self) -> bool:
        return self.llm.configured and not self.sv.settings.offline

    # ================================================================ entry point
    def handle(self, text: str) -> AgentReply:
        text = (text or "").strip()
        if not text:
            return AgentReply("I didn't hear anything.")
        self.sv.state.add("user", text)
        ctx = self.sv.on_main(lambda: build_context(self.sv.engine, self.sv.registry, self.sv.state.selected_part))
        parsed = self.parser.parse(text, ctx)
        log.info("parsed %r as %s %s", text, parsed.kind, [(c.name, c.arguments) for c in parsed.calls])
        if parsed.kind == "command":
            reply = self._commands(parsed)
        elif parsed.kind == "model_request":
            reply = self._model_request(parsed)
        elif parsed.kind == "question":
            reply = self._question(parsed)
        else:
            reply = self._open_request(text)
        self.sv.state.add("assistant", reply.text)
        return reply

    def try_command(self, text: str, accept: tuple[str, ...] = ("command",)) -> AgentReply | None:
        """Run `text` only if the offline parser recognises it as one of
        `accept`; None otherwise, and nothing is touched. For a host (Liza)
        that has its own language model and wants the engine only for the
        commands it is certain about."""
        text = (text or "").strip()
        if not text:
            return None
        ctx = self.sv.on_main(lambda: build_context(self.sv.engine, self.sv.registry, self.sv.state.selected_part))
        parsed = self.parser.parse(text, ctx)
        if parsed.kind not in accept or not parsed.calls:
            return None
        reply = self._model_request(parsed) if parsed.kind == "model_request" else self._commands(parsed)
        self.sv.state.add("user", text)
        self.sv.state.add("assistant", reply.text)
        return reply

    # ================================================================ commands
    def run_calls(self, calls: list[ToolCall]) -> list[ToolResult]:
        results = []
        for call in calls:
            result = self.sv.tools.execute(call)
            results.append(result)
            if result.ok:
                self._track(call)
            else:
                break
        return results

    def _track(self, call: ToolCall) -> None:
        state = self.sv.state
        if call.name in {"transition_to", "zoom_to_level", "go_deeper", "go_up", "go_back", "zoom_into"}:
            # The tool has already recorded where it is going; the engine is
            # still showing where it came from until the transition finishes.
            state.selected_part = None
            if call.name == "zoom_into" and state.current_model != self.sv.engine.model_id:
                return          # the part named belongs to the model being left
        elif call.name in {"load_model", "find_or_acquire", "compare_molecules"}:
            state.select(self.sv.engine.model_id, None)
        part = call.arguments.get("part_id")
        if part and part not in {"all", "none"}:
            state.select(self.sv.engine.model_id, part)

    @staticmethod
    def _say(results: list[ToolResult], hint: str = "") -> str:
        failed = next((r for r in results if not r.ok), None)
        if failed:
            return _friendly_error(failed)
        lines: list[str] = []
        for r in results:
            if r.call.name in {"label_parts", "remove_label"} and len(results) > 1:
                continue            # "Labelled 1 part(s)" adds nothing to "Highlighted X"
            if r.message and r.message not in lines:
                lines.append(r.message)
        return hint or " ".join(lines) or "Done."

    def _commands(self, parsed: ParseResult) -> AgentReply:
        results = self.run_calls(parsed.calls)
        return AgentReply(self._say(results, parsed.reply_hint if all(r.ok for r in results) else ""), results)

    def _model_request(self, parsed: ParseResult) -> AgentReply:
        call = parsed.calls[0]
        if call.name != "model_change":
            results = self.run_calls([call])
            return AgentReply(self._say(results), results)
        target = call.arguments["model_id"]
        current = self.sv.engine.model_id
        chain = self.sv.kg.scale_chain(current) if current else []
        if current and target in chain[1:]:
            real = ToolCall("zoom_to_level", {"target": target})
        elif current and current in self.sv.kg.scale_chain(target)[1:]:
            real = ToolCall("transition_to", {"model_id": target})      # back up the scale chain
        else:
            real = ToolCall("load_model", {"model_id": target})
        results = self.run_calls([real])
        text = self._say(results)
        entry = self.sv.registry.get(target)
        if results and results[-1].ok and entry and "not to scale" in (entry.disclaimer or "").lower():
            text += " It's an educational model, not to scale."
        return AgentReply(text, results)

    # ================================================================ questions
    def _question(self, parsed: ParseResult) -> AgentReply:
        results = self.run_calls(parsed.calls) if parsed.calls else []
        explain = self.sv.tools.execute(ToolCall("explain", {"question": parsed.question,
                                                             **({"concept_id": parsed.topic} if parsed.topic else {})}))
        results.append(explain)
        passages = explain.content.get("passages", []) if explain.ok else []
        if self.llm_available:
            try:
                notes = "\n".join(f"- [{p['concept']} / {p['section']}] {p['text']}" for p in passages[:4])
                desc = explain.content.get("part_description")
                prompt = (f"{parsed.question}\n\nNotes from the curriculum documents (base your answer on these):\n"
                          f"{notes or '(none found)'}" + (f"\nPart description: {desc}" if desc else ""))
                text, llm_results = self._llm_loop(prompt)
                return AgentReply(text, results + llm_results, used_llm=True)
            except LLMError as exc:
                log.warning("LLM unavailable, answering from the notes: %s", exc)
        from knowledge.rag import Chunk
        desc = explain.content.get("part_description")
        generic = re.search(r"\bwhat (?:does|is) (?:this|that|it)\b|\bwhat is this part\b", parsed.question.lower())
        if desc and (generic or not passages):
            return AgentReply(desc, results)
        chunks = [Chunk(p["concept"], p["section"], p["text"], "", "", score=p["score"]) for p in passages]
        return AgentReply(offline_answer(parsed.question, chunks), results)

    # ================================================================ everything else
    def _open_request(self, text: str) -> AgentReply:
        if self.llm_available:
            try:
                reply, results = self._llm_loop(text)
                return AgentReply(reply, results, used_llm=True)
            except LLMError as exc:
                log.warning("LLM unavailable: %s", exc)
        res = self.sv.resolver.resolve(text)
        if res.ok and res.score >= 0.9:
            return self._model_request(ParseResult("model_request", [ToolCall("model_change", {"model_id": res.model_id})]))
        chunks = self.sv.kb.search(text, k=3)
        if chunks and chunks[0].score > 0.3:
            return AgentReply(offline_answer(text, chunks))
        return AgentReply("Sorry, I didn't understand that. You can say things like 'show the heart', "
                          "'rotate it', 'cut it in half' or 'show projectile motion'.")

    # ================================================================ LLM
    def system_prompt(self) -> str:
        summary = self.sv.on_main(self.sv.engine.summary)
        summary["scale"] = self.sv.scale.describe()
        summary["selected_part"] = self.sv.state.selected_part
        models = "\n".join(f"{e.id}: {e.name}" for e in self.sv.registry.entries())
        return PROMPT.read_text(encoding="utf-8").replace("{scene}", json.dumps(summary, ensure_ascii=False, default=str)[:3500]) \
            .replace("{models}", models)

    def _llm_loop(self, user_text: str) -> tuple[str, list[ToolResult]]:
        messages = [{"role": "system", "content": self.system_prompt()}]
        messages += self.sv.state.recent_messages(7)[:-1]
        messages.append({"role": "user", "content": user_text})
        tools = self.sv.tools.openai_tools()
        results: list[ToolResult] = []
        for _ in range(self.sv.settings.llm.max_tool_rounds):
            reply = self.llm.chat(messages, tools)
            if not reply.tool_calls:
                return (reply.content.strip() or self._say(results)), results
            assistant = {"role": "assistant", "content": reply.content or "",
                         "tool_calls": reply.raw_message.get("tool_calls", [])}
            messages.append(assistant)
            for tc in reply.tool_calls:
                result = self.sv.tools.execute(ToolCall(tc.name, tc.arguments, tc.id))
                results.append(result)
                if result.ok:
                    self._track(result.call)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result.to_json()})
        return self._say(results), results


def _friendly_error(result: ToolResult) -> str:
    msg = result.content.get("error", "")
    if msg.startswith("invalid arguments"):
        return "I couldn't do that: " + msg.split(":", 1)[1].strip()
    return msg[0].upper() + msg[1:] + ("" if msg.endswith(".") else ".") if msg else "That didn't work."
