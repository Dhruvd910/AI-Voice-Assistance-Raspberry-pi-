r"""Entry point. The program itself is in assistant.py.

This file stays a shim, and stays called assist.py, for two separate reasons.

THE NAME is what starts it. The `liza` launcher finds a running instance with
    pgrep -f "python.*assist\.py"
and the desktop icons go through that launcher, so renaming this file would
leave `liza stop` and `liza status` unable to see a running Liza.

THE SHIM is what keeps there being one of everything. A file that is both the
script you run AND a module another file imports gets loaded TWICE, under two
names -- once as __main__ and once as itself -- giving two copies of every
queue, every threading.Event and every global. ui.py imports the assistant, so
without this separation the screen would be talking to a second, unheard copy
of the program. It failed loudly the first time (an ImportError on the way back
round the cycle); it would not necessarily fail loudly the next.
"""

import assistant

if __name__ == "__main__":
    assistant.main()
