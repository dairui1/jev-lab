"""Bundle traces/*.jsonl into traces/data.js so viewer.html works from file://."""
import json
from pathlib import Path

ROOT = Path(__file__).parent
TRACES = ROOT / "traces"


def load(name):
    p = TRACES / f"{name}.jsonl"
    if not p.exists():
        return None
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


data = {k: v for k in ("jev", "llm") if (v := load(k))}
(TRACES / "data.js").write_text("window.TRACES = " + json.dumps(data) + ";\n")
print(f"wrote traces/data.js with {', '.join(f'{k}={len(v)}' for k, v in data.items())} events")
