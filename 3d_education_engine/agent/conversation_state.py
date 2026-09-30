"""What the agent remembers between turns: the dialogue, and what "it" means.

"Rotate it", "what does this part do?", "make it transparent" all depend on
context: the model on screen and the part last pointed at. That context lives
here, not in the LLM, so it survives an LLM failure and the offline parser
can use it too.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Turn:
    role: str            # user | assistant | tool
    content: str


@dataclass
class ConversationState:
    current_model: str | None = None
    selected_part: str | None = None          # last part the student named or we highlighted
    # Models reached by zooming into a part, most recent last: zooming back OUT
    # far enough on one of these returns to where it was entered from.
    dives: list[str] = field(default_factory=list)
    history: list[Turn] = field(default_factory=list)
    max_turns: int = 12

    def add(self, role: str, content: str) -> None:
        self.history.append(Turn(role, content))
        del self.history[: -self.max_turns * 2]

    def select(self, model_id: str | None, part_id: str | None) -> None:
        if model_id:
            self.current_model = model_id
        self.selected_part = part_id

    def selected_concept(self) -> str | None:
        if self.current_model and self.selected_part:
            return f"{self.current_model}.{self.selected_part}"
        return self.current_model

    def recent_messages(self, n: int = 8) -> list[dict]:
        return [{"role": t.role, "content": t.content} for t in self.history[-n:] if t.role in {"user", "assistant"}]
