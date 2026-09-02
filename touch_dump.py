#!/usr/bin/env python3
"""Dump raw ADS7846 events, to see exactly what one touch looks like.

    /home/pi1/liza/.venv/bin/python touch_dump.py

Prints a line per touch showing how many samples it carried, the median, and
whether BTN_TOUCH framed it properly. Run it, touch the TOP-LEFT corner, then the
BOTTOM-RIGHT corner, then the CENTRE, lifting cleanly between each, and Ctrl-C.
"""

import re
import select
import struct
import sys
import time

EVENT_FORMAT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)
EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
ABS_X, ABS_Y, ABS_PRESSURE = 0x00, 0x01, 0x18
BTN_TOUCH = 0x14A


def find_device(name="ADS7846"):
    for block in open("/proc/bus/input/devices").read().split("\n\n"):
        if name.lower() in block.lower():
            m = re.search(r"(event\d+)", block)
            if m:
                return "/dev/input/" + m.group(1)
    sys.exit("touch device not found")


def main():
    path = find_device()
    print(f"Reading {path}. Touch TOP-LEFT, then BOTTOM-RIGHT, then CENTRE. "
          f"Ctrl-C when done.\n", flush=True)

    handle = open(path, "rb", buffering=0)
    touch_index = 0
    down = False
    samples = []          # (x, y) pairs seen while down
    x = y = None
    pairs_before_y = 0    # X events that arrived before this touch's first Y
    seen_y_this_touch = False
    last_up = 0.0

    try:
        while True:
            if not select.select([handle], [], [], 0.3)[0]:
                continue
            data = handle.read(EVENT_SIZE)
            if not data or len(data) < EVENT_SIZE:
                continue
            _s, _us, etype, code, value = struct.unpack(EVENT_FORMAT, data)

            if etype == EV_ABS:
                if code == ABS_X:
                    x = value
                    if down and not seen_y_this_touch:
                        pairs_before_y += 1
                elif code == ABS_Y:
                    y = value
                    if down:
                        seen_y_this_touch = True
                if down and x is not None and y is not None:
                    samples.append((x, y))

            elif etype == EV_KEY and code == BTN_TOUCH:
                now = time.time()
                if value:
                    gap = (now - last_up) * 1000 if last_up else -1
                    down = True
                    samples = []
                    seen_y_this_touch = False
                    pairs_before_y = 0
                    touch_index += 1
                    print(f"  DOWN  touch #{touch_index}"
                          f"{f'   {gap:.0f} ms after the last lift' if gap >= 0 else ''}",
                          flush=True)
                else:
                    down = False
                    last_up = now
                    if samples:
                        xs = sorted(s[0] for s in samples)
                        ys = sorted(s[1] for s in samples)
                        mid = len(samples) // 2
                        first, last = samples[0], samples[-1]
                        print(f"  UP    touch #{touch_index}: {len(samples):4d} samples"
                              f" | median=({xs[mid]:5d},{ys[mid]:5d})"
                              f" | first={first} last={last}"
                              f" | X-before-first-Y={pairs_before_y}", flush=True)
                    else:
                        print(f"  UP    touch #{touch_index}: NO SAMPLES", flush=True)
    except KeyboardInterrupt:
        print("\nDone.", flush=True)
    finally:
        handle.close()


if __name__ == "__main__":
    main()
