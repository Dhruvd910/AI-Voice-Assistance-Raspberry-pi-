"""Entry point.

    python -m app.main                         the touch-screen app (PySide6)
    python -m app.main --say "show the heart" --say "cut it in half" --screenshot out.png
                                               run commands with no window, save the frame
    python -m app.main --repl                  type commands, see replies (screenshots on request)
    python -m app.main --check                 load everything, report problems, exit
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import load_settings, prepare_gl_environment  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="3D Education Engine")
    parser.add_argument("--say", action="append", default=[], help="run a command/question (repeatable)")
    parser.add_argument("--screenshot", help="save the final frame to this PNG (with --say)")
    parser.add_argument("--repl", action="store_true", help="interactive text mode")
    parser.add_argument("--check", action="store_true", help="load everything and report")
    parser.add_argument("--rebuild-index", action="store_true", help="rebuild graph, RAG index and ATTRIBUTIONS.md")
    parser.add_argument("--offline", action="store_true", help="never use the network (no LLM, no fetching)")
    parser.add_argument("--windowed", action="store_true", help="do not go full screen")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    prepare_gl_environment()
    settings = load_settings()
    if args.offline:
        settings.offline = True
    if args.windowed:
        settings.fullscreen = False

    if args.say or args.repl or args.check:
        from app.dependency_container import build
        sv = build(settings, interactive=False, rebuild_index=True if args.rebuild_index else None)
        if args.check:
            print(f"{len(sv.registry.ids())} models, {len(sv.tools.names())} tools, "
                  f"{len(sv.repo.chunks())} knowledge passages, LLM: "
                  f"{'configured (' + settings.llm.model + ')' if sv.agent.llm_available else 'not configured (offline parser only)'}")
            return 0
        for line in args.say:
            reply = sv.agent.handle(line)
            sv.engine.finish_transition()
            print(f"> {line}\n  {reply.text}")
        if args.repl:
            print("Type a command ('screenshot FILE' saves the view, 'quit' exits).")
            while True:
                try:
                    line = input("> ").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if line in {"quit", "exit"}:
                    break
                if line.startswith("screenshot "):
                    print("  saved", sv.engine.screenshot(line.split(" ", 1)[1]))
                    continue
                reply = sv.agent.handle(line)
                sv.engine.finish_transition()
                print(f"  {reply.text}")
        if args.screenshot:
            sv.engine.screenshot(args.screenshot)
            print(f"saved {args.screenshot}")
        return 0

    from ui.main_window import run_app
    return run_app(settings, rebuild_index=args.rebuild_index)


if __name__ == "__main__":
    sys.exit(main())
