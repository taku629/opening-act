"""Record a run on SYNTHETIC demo data into static/demo-trace.json (used by the
static replay mode). Refuses to run against the live API so that no real Qloo
response data ever ends up in the public repository."""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["QLOO_MOCK"] = "1"
os.environ["LLM_DISABLED"] = "1"
os.environ.setdefault("CACHE_PATH", "/tmp/opening_act_record.sqlite")

from app.agent import Agent  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.qloo_client import QlooClient  # noqa: E402


async def main(headliner="Japanese Breakfast", city="Chicago", capacity=400):
    s = get_settings()
    assert s.qloo_mock, "refusing to record live Qloo data"
    events = []

    async def emit(ev):
        events.append(ev)

    q = QlooClient(s)
    await Agent(s, q).run(headliner, city, capacity, None, emit)
    for ev in events:
        if ev["type"] == "qloo_call":
            ev["duration_ms"] = max(ev["duration_ms"], 90)
    out = Path(__file__).resolve().parent.parent / "static" / "demo-trace.json"
    out.write_text(json.dumps(events, default=str))
    print(f"wrote {len(events)} events -> {out}")


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:3]) if len(sys.argv) >= 3 else main())
