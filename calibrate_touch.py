#!/usr/bin/env python3
"""Touchscreen calibration for the ADS7846 resistive panel.

Shows a target, you touch it, repeat. From the raw ADC readings it fits the
six-element libinput calibration matrix and prints the labwc config to paste in.

    /home/pi1/liza/.venv/bin/python calibrate_touch.py

WHY RAW EVENTS RATHER THAN MOUSE CLICKS. This reads /dev/input/event* directly,
so it sees the panel's own ADC values before the compositor has transformed
anything. That makes the measurement independent of whatever calibration is
already in place -- including a badly wrong one -- so this can be re-run to
correct itself without first undoing anything. Reading clicks out of Tk instead
would measure the transform on top of itself and converge on nothing.

A resistive panel is noisiest as the pen lands and as it lifts, so each touch is
reduced to the median of its middle samples rather than to any single reading.
"""

import os
import re
import select
import struct
import sys
import threading
import time
import tkinter as tk

# struct input_event on 64-bit Linux: struct timeval (2 x long), __u16 type,
# __u16 code, __s32 value.
EVENT_FORMAT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)

EV_KEY, EV_ABS = 0x01, 0x03
ABS_X, ABS_Y, ABS_PRESSURE = 0x00, 0x01, 0x18
BTN_TOUCH = 0x14A

DEVICE_NAME = "ADS7846 Touchscreen"

# Inset from the edges. Far enough out to pin down the scale, not so far that a
# resistive panel's non-linear corners dominate the fit.
TARGETS = [
    (0.12, 0.15, "top left"),
    (0.88, 0.15, "top right"),
    (0.88, 0.85, "bottom right"),
    (0.12, 0.85, "bottom left"),
    (0.50, 0.50, "the centre"),
]


def find_touch_device(name=DEVICE_NAME):
    """The /dev/input/eventN for the panel. Its number moves between boots."""
    try:
        blocks = open("/proc/bus/input/devices").read().split("\n\n")
    except OSError as exc:
        sys.exit(f"Cannot read /proc/bus/input/devices: {exc}")
    for block in blocks:
        if name.lower() not in block.lower():
            continue
        match = re.search(r"(event\d+)", block)
        if match:
            return "/dev/input/" + match.group(1)
    sys.exit(f"No input device named {name!r}. Is the panel connected?")


def abs_range(path, code):
    """(min, max) for one absolute axis, straight from the driver."""
    import fcntl
    size = struct.calcsize("6i")
    request = (2 << 30) | (size << 16) | (ord("E") << 8) | (0x40 + code)
    with open(path, "rb") as handle:
        buf = bytearray(size)
        fcntl.ioctl(handle, request, buf)
        _value, minimum, maximum, _fuzz, _flat, _res = struct.unpack("6i", bytes(buf))
    return minimum, maximum


def solve3(matrix, rhs):
    """Gaussian elimination with partial pivoting on a 3x3. None if singular."""
    rows = [list(matrix[i]) + [rhs[i]] for i in range(3)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(rows[r][col]))
        if abs(rows[pivot][col]) < 1e-12:
            return None
        rows[col], rows[pivot] = rows[pivot], rows[col]
        for r in range(3):
            if r == col:
                continue
            factor = rows[r][col] / rows[col][col]
            for c in range(col, 4):
                rows[r][c] -= factor * rows[col][c]
    return [rows[i][3] / rows[i][i] for i in range(3)]


def fit_matrix(samples):
    """Least-squares fit of normalised device coords to normalised screen coords.

    Returns (a, b, c, d, e, f) such that
        screen_x = a*device_x + b*device_y + c
        screen_y = d*device_x + e*device_y + f
    which is exactly libinput's calibration matrix. Fitting all six terms rather
    than a per-axis scale is what lets one pass absorb a swapped or inverted axis
    as well as the scale -- they are all just entries in this matrix.
    """
    sum_uu = sum_uv = sum_vv = sum_u = sum_v = 0.0
    rhs_x = [0.0, 0.0, 0.0]
    rhs_y = [0.0, 0.0, 0.0]
    for u, v, sx, sy in samples:
        sum_uu += u * u
        sum_uv += u * v
        sum_vv += v * v
        sum_u += u
        sum_v += v
        rhs_x[0] += u * sx
        rhs_x[1] += v * sx
        rhs_x[2] += sx
        rhs_y[0] += u * sy
        rhs_y[1] += v * sy
        rhs_y[2] += sy
    normal = [[sum_uu, sum_uv, sum_u],
              [sum_uv, sum_vv, sum_v],
              [sum_u, sum_v, float(len(samples))]]
    row_x = solve3(normal, rhs_x)
    row_y = solve3(normal, rhs_y)
    if row_x is None or row_y is None:
        return None
    return tuple(row_x) + tuple(row_y)


# Measured on this panel with touch_dump.py, not guessed:
#
#  * ONE physical press arrives as MANY BTN_TOUCH down/up cycles -- the dump
#    showed repeated "DOWN 10 ms after the last lift" runs. Treating each cycle
#    as its own touch reads one bouncing fragment, and the shortest fragments
#    carried as few as three samples. So contacts closer together than
#    BOUNCE_GAP are joined into a single stroke.
#  * The driver emits ABS_X before the first ABS_Y of every touch (the dump's
#    X-before-first-Y was 1 every single time). Pairing that early X with the y
#    variable still holding the LAST touch's value put a foreign point at the
#    head of every stroke -- visible in the dump as touch N's `first` y matching
#    touch N-1's `last` y exactly. Both coordinates are therefore reset on
#    pen-down and no sample is taken until this stroke has supplied its own Y.
BOUNCE_GAP = 0.25      # contacts closer than this are one press, not two
SETTLE = 0.45          # quiet for this long and the press is finished
MIN_SAMPLES = 8        # below this the median is noise, so keep waiting


class TouchReader(threading.Thread):
    """Raw ADC readings, accumulated per press rather than per contact."""

    def __init__(self, path):
        super().__init__(daemon=True)
        self.path = path
        self.lock = threading.Lock()
        self.stroke = []        # every sample of the press in progress
        self.live = None        # most recent reading, for the on-screen readout
        self.contact = False    # pen is down right now
        self.last_release = 0.0
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def arm(self):
        """Forget everything: a new target is being shown."""
        with self.lock:
            self.stroke = []
            self.last_release = 0.0

    def take(self):
        """The finished press, or None while one is still in progress.

        A press counts as finished once nothing has touched the panel for
        SETTLE seconds, which is what lets a bouncing contact be gathered up as
        the single press it physically was.
        """
        with self.lock:
            if self.contact or not self.stroke:
                return None
            if len(self.stroke) < MIN_SAMPLES:
                return None
            if time.time() - self.last_release < SETTLE:
                return None
            samples, self.stroke = self.stroke, []
            return self._reduce(samples)

    def pending(self):
        with self.lock:
            return len(self.stroke)

    def run(self):
        try:
            handle = open(self.path, "rb", buffering=0)
        except PermissionError:
            print(f"[TOUCH] Permission denied on {self.path}. "
                  f"Add yourself to the 'input' group.", flush=True)
            return
        x = None
        y = None
        with handle:
            while not self._stop.is_set():
                # Timed, so the thread notices stop() on an idle panel.
                if not select.select([handle], [], [], 0.2)[0]:
                    continue
                data = handle.read(EVENT_SIZE)
                if not data or len(data) < EVENT_SIZE:
                    continue
                _sec, _usec, etype, code, value = struct.unpack(EVENT_FORMAT, data)

                if etype == EV_ABS:
                    if code == ABS_X:
                        x = value
                    elif code == ABS_Y:
                        y = value
                    # y is None until THIS stroke has reported one, so the
                    # leading X can never be paired with the last press's Y.
                    if x is not None and y is not None:
                        self.live = (x, y)
                        if self.contact:
                            with self.lock:
                                self.stroke.append((x, y))

                elif etype == EV_KEY and code == BTN_TOUCH:
                    now = time.time()
                    if value:
                        x = y = None       # see the note above BOUNCE_GAP
                        with self.lock:
                            if (self.last_release
                                    and now - self.last_release > BOUNCE_GAP):
                                # A genuinely separate press: drop the old one
                                # rather than averaging two places together.
                                self.stroke = []
                            self.contact = True
                    else:
                        with self.lock:
                            self.contact = False
                            self.last_release = now

    @staticmethod
    def _reduce(samples):
        """Median of the middle half of the press.

        The pen landing and the pen lifting are where a resistive panel reports
        its worst values -- pressure is still building, so the divider is not yet
        settled. Dropping the first and last quarter throws those away. The
        median rather than the mean because a bounce leaves outliers a mean
        would follow.
        """
        start, end = len(samples) // 4, max(len(samples) * 3 // 4,
                                            len(samples) // 4 + 1)
        middle = samples[start:end]
        xs = sorted(s[0] for s in middle)
        ys = sorted(s[1] for s in middle)
        return xs[len(xs) // 2], ys[len(ys) // 2]


class CalibrationUI:
    def __init__(self, root, reader, x_range, y_range):
        self.root = root
        self.reader = reader
        self.x_range = x_range
        self.y_range = y_range
        self.index = 0
        self.samples = []

        self.width = root.winfo_screenwidth()
        self.height = root.winfo_screenheight()
        root.attributes("-fullscreen", True)
        root.configure(bg="#101322")
        self.canvas = tk.Canvas(root, width=self.width, height=self.height,
                                bg="#101322", highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        root.bind("<Escape>", lambda e: self.abort())
        self.draw()
        self.poll()

    def target_xy(self):
        fx, fy, _ = TARGETS[self.index]
        return fx * self.width, fy * self.height

    def draw(self):
        self.canvas.delete("all")
        _fx, _fy, where = TARGETS[self.index]
        cx, cy = self.target_xy()
        self.canvas.create_text(
            self.width / 2, 40,
            text=f"Touch {where}   ({self.index + 1} of {len(TARGETS)})",
            fill="#FFFFFF", font=("DejaVu Sans", 17, "bold"))
        self.canvas.create_text(
            self.width / 2, 70,
            text="Press firmly on the centre of the cross and HOLD for about a "
                 "second, then lift.   Esc to cancel.",
            fill="#8891A8", font=("DejaVu Sans", 10))
        for radius, colour in ((26, "#2A3350"), (16, "#4C6FFF")):
            self.canvas.create_oval(cx - radius, cy - radius, cx + radius,
                                    cy + radius, outline=colour, width=2)
        self.canvas.create_line(cx - 34, cy, cx + 34, cy, fill="#4C6FFF", width=2)
        self.canvas.create_line(cx, cy - 34, cx, cy + 34, fill="#4C6FFF", width=2)
        self.canvas.create_oval(cx - 3, cy - 3, cx + 3, cy + 3,
                                fill="#FF4D6D", outline="")
        self.status = self.canvas.create_text(
            self.width / 2, self.height - 34, text="Waiting for a touch...",
            fill="#5A6480", font=("DejaVu Sans", 9))

    def poll(self):
        live = self.reader.live
        held = self.reader.pending()
        if live is not None:
            self.canvas.itemconfig(
                self.status,
                text=f"raw x={live[0]:5d}  y={live[1]:5d}"
                     + (f"   holding, {held} samples" if self.reader.contact
                        else (f"   {held} samples -- keep holding"
                              if 0 < held < MIN_SAMPLES else "")))
        done = self.reader.take()
        if done is not None:
            self.record(done)
            return
        self.root.after(40, self.poll)

    def record(self, raw):
        fx, fy, where = TARGETS[self.index]
        x_min, x_max = self.x_range
        y_min, y_max = self.y_range
        u = (raw[0] - x_min) / float(x_max - x_min)
        v = (raw[1] - y_min) / float(y_max - y_min)
        self.samples.append((u, v, fx, fy))
        print(f"[POINT] {where:14} raw=({raw[0]:5d},{raw[1]:5d})  "
              f"device=({u:.4f},{v:.4f})  target=({fx:.2f},{fy:.2f})", flush=True)
        self.index += 1
        if self.index >= len(TARGETS):
            self.root.after(150, self.finish)
        else:
            # Long enough that the tail of this press -- a resistive panel goes
            # on bouncing for a while after the finger leaves -- cannot be
            # collected against the NEXT target. arm() then throws away whatever
            # did arrive in the meantime, so the next reading starts clean.
            self.canvas.itemconfig(self.status, text="Got it.")
            self.root.after(900, self.next_target)

    def next_target(self):
        self.reader.arm()
        self.draw()
        self.poll()

    def finish(self):
        self.reader.stop()
        self.root.destroy()
        report(self.samples, self.width, self.height)

    def abort(self):
        self.reader.stop()
        self.root.destroy()
        print("\nCancelled; nothing was changed.", flush=True)


def report(samples, width, height):
    matrix = fit_matrix(samples)
    if matrix is None:
        sys.exit("Could not fit a matrix -- the touches were degenerate. "
                 "Re-run and press firmly on each cross.")
    a, b, c, d, e, f = matrix

    print("\n" + "=" * 62)
    worst = 0.0
    for u, v, sx, sy in samples:
        px = (a * u + b * v + c) * width
        py = (d * u + e * v + f) * height
        error = ((px - sx * width) ** 2 + (py - sy * height) ** 2) ** 0.5
        worst = max(worst, error)
    print(f"Fit residual: worst point is {worst:.1f} px out "
          f"({'good' if worst < 12 else 'high -- consider re-running'}).")

    swapped = abs(b) > abs(a)
    print(f"Axes look {'SWAPPED (x/y crossed)' if swapped else 'not swapped'}; "
          f"x {'inverted' if (b if swapped else a) < 0 else 'normal'}, "
          f"y {'inverted' if (d if swapped else e) < 0 else 'normal'}.")

    values = " ".join(f"{v:.6f}" for v in matrix)
    print("\nCalibration matrix:\n  " + values)
    print("\nAdd this inside <openbox_config> in ~/.config/labwc/rc.xml:\n")
    print('  <libinput>')
    print(f'    <device category="{DEVICE_NAME}">')
    print(f'      <calibrationMatrix>{values}</calibrationMatrix>')
    print('    </device>')
    print('  </libinput>')
    print("\nThen apply it with:  labwc --reconfigure")
    print("=" * 62)

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "touch_calibration.txt")
    try:
        with open(out, "w") as handle:
            handle.write(values + "\n")
        print(f"(also written to {out})")
    except OSError:
        pass


class VerifyUI:
    """Where a tap ACTUALLY lands, after the compositor's matrix.

    The calibration pass reads the panel directly and so is blind to whether the
    matrix was applied. This one measures the opposite end of the chain -- the
    click coordinates a normal application receives -- so it reports the error a
    user of the finished device would feel, and it is the only check that can
    tell "the matrix is wrong" apart from "the matrix never got loaded".
    """

    def __init__(self, root):
        self.root = root
        self.index = 0
        self.errors = []
        self.width = root.winfo_screenwidth()
        self.height = root.winfo_screenheight()
        root.attributes("-fullscreen", True)
        root.configure(bg="#101322")
        self.canvas = tk.Canvas(root, width=self.width, height=self.height,
                                bg="#101322", highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self.hit)
        root.bind("<Escape>", lambda e: root.destroy())
        # Bounce reaches this end of the chain too. mouseEmulation turns every
        # BTN_TOUCH into a click, and touch_dump.py showed one press producing
        # contacts 10-30 ms apart, so a single finger press arrives here as a
        # burst of clicks scattered along the path the contact wandered. Taking
        # the first of them measured the noisiest instant of the press, and a
        # burst still arriving when the next target was drawn was credited to
        # THAT target -- which is the 628 px "error" on a tap nobody made.
        self.clicks = []
        self.armed_at = 0.0
        self.draw()

    def draw(self):
        self.canvas.delete("all")
        self.clicks = []
        self.armed_at = time.time()
        fx, fy, where = TARGETS[self.index]
        cx, cy = fx * self.width, fy * self.height
        self.canvas.create_text(self.width / 2, 40,
                                text=f"Tap {where}   ({self.index + 1} of {len(TARGETS)})",
                                fill="#FFFFFF", font=("DejaVu Sans", 17, "bold"))
        self.canvas.create_text(self.width / 2, 70,
                                text="Checking the calibration. A single tap is enough.",
                                fill="#8891A8", font=("DejaVu Sans", 10))
        self.canvas.create_line(cx - 30, cy, cx + 30, cy, fill="#22C55E", width=2)
        self.canvas.create_line(cx, cy - 30, cx, cy + 30, fill="#22C55E", width=2)
        self.canvas.create_oval(cx - 18, cy - 18, cx + 18, cy + 18,
                                outline="#22C55E", width=2)

    # A click arriving within this of the target appearing is the tail of the
    # PREVIOUS press, not an answer to this one.
    GUARD = 0.6
    # ...and the burst is finished once this long passes with no further click.
    QUIET = 0.5

    def hit(self, event):
        now = time.time()
        if now - self.armed_at < self.GUARD:
            print(f"[CHECK] ignored a click {(now - self.armed_at) * 1000:.0f} ms "
                  f"after the target appeared (bounce from the last press)",
                  flush=True)
            return
        self.clicks.append((event.x, event.y))
        self.root.after(int(self.QUIET * 1000), self.settle)

    def settle(self):
        """Score the press once its burst of clicks has stopped arriving."""
        if not self.clicks:
            return
        pending = len(self.clicks)
        self.root.after(60, lambda: self._score(pending))

    def _score(self, seen):
        if not self.clicks or len(self.clicks) != seen:
            return          # more clicks landed; a later settle() will score it
        xs = sorted(c[0] for c in self.clicks)
        ys = sorted(c[1] for c in self.clicks)
        px, py = xs[len(xs) // 2], ys[len(ys) // 2]
        self.clicks = []

        fx, fy, where = TARGETS[self.index]
        cx, cy = fx * self.width, fy * self.height
        dx, dy = px - cx, py - cy
        error = (dx * dx + dy * dy) ** 0.5
        self.errors.append((where, dx, dy, error))
        print(f"[CHECK] {where:14} tapped ({px:4d},{py:4d}) "
              f"target ({cx:.0f},{cy:.0f})  off by {dx:+.0f},{dy:+.0f} = {error:.0f} px"
              f"   [{seen} click{'s' if seen != 1 else ''} merged]", flush=True)
        self.index += 1
        if self.index >= len(TARGETS):
            self.root.after(200, self.finish)
        else:
            self.root.after(900, self.draw)

    def finish(self):
        self.root.destroy()
        worst = max(e[3] for e in self.errors)
        mean = sum(e[3] for e in self.errors) / len(self.errors)
        print("\n" + "=" * 56)
        print(f"Average miss {mean:.0f} px, worst {worst:.0f} px.")
        if worst < 15:
            print("Calibration is good.")
        elif worst < 30:
            print("Usable, but re-running the calibration may tighten it.")
        else:
            print("Still well out. Re-run the calibration, pressing precisely\n"
                  "on each cross -- or the matrix may not have been loaded.")
        print("=" * 56)


def main():
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        os.environ["DISPLAY"] = ":0"

    if "--verify" in sys.argv:
        root = tk.Tk()
        root.title("Touch calibration check")
        VerifyUI(root)
        root.mainloop()
        return

    path = find_touch_device()
    x_range = abs_range(path, ABS_X)
    y_range = abs_range(path, ABS_Y)
    print(f"[TOUCH] {DEVICE_NAME} on {path}", flush=True)
    print(f"[TOUCH] raw range x={x_range} y={y_range}", flush=True)

    reader = TouchReader(path)
    reader.start()

    root = tk.Tk()
    root.title("Touch calibration")
    CalibrationUI(root, reader, x_range, y_range)
    root.mainloop()


if __name__ == "__main__":
    main()
