"""Calling the screen from a thread that is not the screen's.

Tk may only be touched from the thread that built the window, so every update
raised by the ai loop, the media watcher or a background worker has to be
handed across. Both of these do that, and both find the screen through
state.ui_instance rather than being given it, so that the media player and the
action tags can reach the UI without importing the assistant.

They differ in what they do with no display attached, and the difference is
deliberate -- see ui_invoke.
"""

import state


def ui_call(callback):
    """Run a UI update on the Tk thread. No-op when headless."""
    if state.ui_instance is None:
        return
    root = getattr(state.ui_instance, "root", None)
    if root is not None:
        root.after(0, callback)


def ui_invoke(method_name, *args):
    """Call a UI method on the Tk thread, or directly when there is no Tk.

    ui_call() drops everything in headless mode, which is right for repainting
    and wrong for these: going to sleep has to work with no screen attached."""
    target = state.ui_instance
    if target is None:
        return
    fn = getattr(target, method_name, None)
    if fn is None:
        return
    root = getattr(target, "root", None)
    if root is not None:
        root.after(0, lambda: fn(*args))
    else:
        fn(*args)
