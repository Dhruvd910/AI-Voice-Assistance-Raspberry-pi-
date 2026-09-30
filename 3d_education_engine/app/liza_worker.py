"""The engine as a drawing worker for Liza.

Liza's viewer3d.Renderer runs its drawing in a separate process and talks to
it over stdin/stdout: each message is a 4-byte big-endian length and a pickled
batch of commands, and each batch gets exactly one reply. This module speaks
that same protocol, so Liza's board, touch handling and zoom buttons drive the
engine unchanged:

    ("show", scene, (w, h))      load scene["model_id"] at that size
    ("resize", (w, h))
    ("turn", d_azimuth, d_elevation)
    ("zoom", factor)             zoomed in far enough on a part that has a model of
                                 its own, this dives into it (and back out again);
                                 the reply's answers then carry ZOOM_EVENT
    ("reset",)
    ("tick", seconds)            transitions, heartbeats, simulations
    ("ask", token, op, text)     op "try": run text only if it is a certain command
                                 op "do":  run an instruction from Liza's model
                                 op "state": describe the scene

Reply: ("frame", w, h, rgb_bytes, answers), ("ok", answers) or ("error", reason),
where answers maps each ask's token to a dict.

Run by Liza with this project's own interpreter:
    <engine>/.venv/bin/python -m app.liza_worker
"""

from __future__ import annotations

import logging
import os
import pickle
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import load_settings, prepare_gl_environment  # noqa: E402

prepare_gl_environment()


# Not an ask's token: an answer Liza did not ask for, saying the model changed.
ZOOM_EVENT = "zoom"


def read_message(stream):
    head = stream.read(4)
    if len(head) < 4:
        raise EOFError
    (length,) = struct.unpack("!I", head)
    data = stream.read(length)
    if len(data) < length:
        raise EOFError
    return data


class LizaWorker:
    def __init__(self):
        settings = load_settings()
        settings.offline = True                 # Liza has her own language model
        settings.fullscreen = False
        from app.dependency_container import build
        self.sv = build(settings, interactive=True)
        self.engine = self.sv.engine
        self.engine.overlay_mode = "attribution"
        self.engine.auto_labels = True

    # ------------------------------------------------------------ asks
    def board_text(self, target: str | None = None) -> str:
        """One line for Liza's ON_BOARD: what is up and what has been done to it.

        `target` is where a transition is heading: the engine still shows the
        old model until it lands, but ON_BOARD must already name the new one."""
        s = self.engine.summary()
        if target and target != s.get("model"):
            entry = self.sv.registry.get(target)
            if entry is not None:
                parts = [p["name"] for p in entry.parts.values() if p.get("available", True)]
                return (f"3D model of {entry.name} (the 3D engine's"
                        + (f"; parts: {', '.join(parts[:16])}" if parts else "") + ")")
        if not s.get("model"):
            return ""
        parts = s["parts"]
        bits = [f"3D model of {s['name']} (the 3D engine's; parts: "
                + ", ".join(p["name"] for p in list(parts.values())[:16]) + ")"]
        lit = [p["name"] for p in parts.values() if p["highlighted"]]
        hidden = [p["name"] for p in parts.values() if not p["visible"]]
        if lit:
            bits.append("highlighted: " + ", ".join(lit))
        if hidden:
            bits.append("hidden: " + ", ".join(hidden[:8]))
        if s.get("clip"):
            bits.append("cut open")
        if s.get("exploded"):
            bits.append("pulled apart")
        sim = s.get("simulation")
        if sim:
            bits.append("settings: " + ", ".join(f"{k} = {v['value']:g} {v['unit']}".strip()
                                                 for k, v in sim["parameters"].items()))
            bits.append("results: " + "; ".join(f"{k.replace('_', ' ')} {v}" for k, v in sim["measurements"].items()))
        return "; ".join(bits)

    def _moved_answer(self, before: str | None, reply_text: str) -> dict:
        target = self.sv.state.current_model or self.engine.model_id
        entry = self.sv.registry.get(target) if target else None
        answer = {"handled": True, "reply": reply_text, "board": self.board_text(target),
                  "model_id": target, "changed_model": target != before}
        if entry is not None:
            answer.update({"title": entry.name, "note": entry.disclaimer or "",
                           "spin": entry.kind != "simulation"})
        return answer

    def zoomed(self, factor: float) -> dict | None:
        """The student zoomed with the buttons. The zoom tool itself dives into
        a part's model, or back out of one, when they go far enough (see
        toolset.zoom); this only tells Liza when that happened."""
        from agent.tool_registry import ToolCall
        eng = self.engine
        before, moving = eng.model_id, eng.transition is not None
        result = self.sv.tools.execute(ToolCall("zoom", {"amount": factor}))
        if not result.ok or moving or eng.transition is None:
            return None
        self.sv.state.selected_part = None
        return self._moved_answer(before, result.message)

    def ask(self, op: str, text: str) -> dict:
        agent = self.sv.agent
        before = self.engine.model_id
        if op == "state":
            return {"handled": True, "board": self.board_text(), "model_id": before}
        accept = ("command",) if op == "try" else ("command", "model_request")
        reply = agent.try_command(text, accept=accept)
        if reply is None:
            return {"handled": False, "reason": "not a command the engine recognises"}
        target = self.sv.state.current_model or self.engine.model_id
        entry = self.sv.registry.get(target) if target else None
        answer = {"handled": reply.ok, "reply": reply.text, "board": self.board_text(target),
                  "model_id": target, "changed_model": target != before}
        if entry is not None:
            answer.update({"title": entry.name, "note": entry.disclaimer or "",
                           "spin": entry.kind != "simulation"})
        if not reply.ok:
            answer["reason"] = reply.text
        return answer

    # ------------------------------------------------------------ one batch
    def run(self, batch) -> tuple:
        eng = self.engine
        answers: dict = {}
        dirty = False
        for command in batch:
            name = command[0]
            if name == "show":
                scene, size = command[1], command[2]
                eng.resize(*size)
                from agent.tool_registry import ToolCall
                result = self.sv.tools.execute(ToolCall("load_model", {"model_id": scene["model_id"]}))
                if not result.ok:
                    return ("error", f"the engine could not load {scene.get('title')}: {result.message}")
                # Running from the start, as Liza's own models are: a solar
                # system whose planets sit still until someone says "play"
                # looked broken ("pause" and the sliders still work).
                if eng.simulation is not None:
                    eng.simulation.playing = True
                dirty = True
            elif eng.scene is None:
                if name == "ask":
                    answers[command[1]] = {"handled": False, "reason": "no engine model is loaded"}
                continue
            elif name == "resize":
                eng.resize(*command[1])
                eng.refresh_overlays()
                dirty = True
            elif name == "turn":
                cam = eng.plotter.camera
                cam.Azimuth(command[1])
                cam.Elevation(command[2])
                cam.OrthogonalizeViewUp()
                eng.plotter.renderer.ResetCameraClippingRange()
                dirty = True
            elif name == "zoom":
                moved = self.zoomed(float(command[1]))
                if moved:
                    answers[ZOOM_EVENT] = moved
                dirty = True
            elif name == "reset":
                eng.reset_camera()
                dirty = True
            elif name == "tick":
                dirty = eng.tick(float(command[1])) or dirty
            elif name == "ask":
                token, op, text = command[1], command[2], command[3]
                try:
                    answers[token] = self.ask(op, text)
                except Exception as exc:
                    logging.exception("ask failed")
                    answers[token] = {"handled": False, "reason": f"engine error: {exc}"}
                dirty = True
        if dirty and eng.scene is not None:
            image = eng.render()
            return ("frame", image.shape[1], image.shape[0], image[:, :, :3].tobytes(), answers)
        return ("ok", answers)


def main() -> None:
    # stdout carries replies and nothing else: stray prints from VTK go to stderr.
    replies = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="[3D engine] %(levelname)s %(message)s")
    commands = sys.stdin.buffer
    worker = None
    while True:
        try:
            batch = pickle.loads(read_message(commands))
        except EOFError:
            break
        try:
            if worker is None:
                worker = LizaWorker()
            reply = worker.run(batch)
        except Exception as exc:
            logging.exception("batch failed")
            reply = ("error", f"the 3D engine could not draw this ({exc})")
        data = pickle.dumps(reply)
        try:
            replies.write(struct.pack("!I", len(data)) + data)
            replies.flush()
        except BrokenPipeError:
            break           # Liza switched to her own drawer and closed us: not an error


if __name__ == "__main__":
    main()
