#!/bin/sh
# Start the 3D Education Engine on the Pi's screen.
cd "$(dirname "$0")" && DISPLAY="${DISPLAY:-:0}" exec .venv/bin/python -m app.main "$@"
