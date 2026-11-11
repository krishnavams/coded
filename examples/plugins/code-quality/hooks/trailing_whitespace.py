"""PostToolUse hook: report trailing whitespace in the file that was just edited.

Exit code 2 sends the message back to the model so it can fix the lines.
"""
import json
import os
import sys

payload = json.load(sys.stdin)
path = (payload.get("tool_input") or {}).get("path")
if not path:
    sys.exit(0)
if not os.path.isabs(path):
    path = os.path.join(payload.get("cwd", "."), path)
try:
    with open(path, encoding="utf-8") as fh:
        bad = [i for i, line in enumerate(fh, 1) if line.rstrip("\n") != line.rstrip("\n").rstrip()]
except (OSError, UnicodeDecodeError):
    sys.exit(0)
if bad:
    shown = ", ".join(map(str, bad[:20])) + (" …" if len(bad) > 20 else "")
    print(f"{payload.get('tool_input', {}).get('path')}: trailing whitespace on line(s) {shown}. "
          "Please remove it.", file=sys.stderr)
    sys.exit(2)
