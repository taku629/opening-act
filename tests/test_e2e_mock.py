"""End-to-end tests in demo (mock) mode: HTTP API, SSE stream, swap, export."""
import json

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
ALL_STEPS = {"resolve", "openers", "demographics", "heatmap", "venues", "brands", "momentum", "overlap"}


def parse_sse(text):
    out = []
    for chunk in text.split("\n\n"):
        data = [l[5:].strip() for l in chunk.split("\n") if l.startswith("data:")]
        if data:
            out.append(json.loads("\n".join(data)))
    return out


def test_health_reports_demo_mode():
    h = client.get("/api/health").json()
    assert h["ok"] and h["mock"] is True and h["llm"] is False
    assert h["qloo_base_url"] == "https://hackathon.api.qloo.com"


def test_index_served():
    r = client.get("/")
    assert r.status_code == 200 and "Opening Act" in r.text


def test_full_brief_json():
    r = client.post("/api/brief", json={"headliner": "Japanese Breakfast", "city": "Chicago", "capacity": 400,
                                        "show_date": "2027-03-14"})
    assert r.status_code == 200
    b = r.json()
    assert b["mock"] is True
    assert b["headliner"]["name"] == "Japanese Breakfast"
    assert len(b["openers"]) >= 5
    assert all(0 <= o["affinity"] <= 1 for o in b["openers"])
    assert all(o["popularity"] <= b["headliner"]["popularity"] + 1e-9 for o in b["openers"])  # nobody bigger
    assert b["demographics"]["top_age"]
    assert b["heatmap"]["neighborhoods"] and b["heatmap"]["named"] is True
    assert abs(sum(n["share"] for n in b["heatmap"]["neighborhoods"]) - 1) < 0.35
    assert b["venues"] and b["brands"]
    assert b["overlap"]["shared_tags"]
    assert b["selected_opener"]["name"] in b["pitch_md"]
    assert "demo data" in b["pitch_md"].lower()
    endpoints = {c["endpoint"] for c in b["calls"]}
    assert {"/search", "/v2/insights", "/v2/tags", "/v2/trending", "/v2/analysis/compare"} <= endpoints
    assert not any(c["endpoint"].startswith(("/recs", "/recommendations")) for c in b["calls"])
    types = {c["params"].get("filter.type") for c in b["calls"]}
    assert {"urn:entity:artist", "urn:demographics", "urn:heatmap", "urn:entity:place", "urn:entity:brand",
            "urn:tag"} <= types
    assert all(c["stripped_params"] == [] for c in b["calls"])  # we only send documented params


def test_sse_stream_and_swap_and_markdown():
    r = client.get("/api/brief/stream", params={"headliner": "Mitski", "city": "New York", "capacity": 300})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    evs = parse_sse(r.text)
    kinds = [e["type"] for e in evs]
    assert kinds[0] == "run_start" and kinds[-1] == "done"
    assert {e["step"] for e in evs if e["type"] == "step_done"} == ALL_STEPS
    assert sum(1 for k in kinds if k == "qloo_call") >= 10
    assert "pitch" in kinds
    brief = evs[-1]["brief"]
    run_id = brief["run_id"]

    # swap to another opener and regenerate the pitch
    other = next(o for o in brief["openers"] if o["id"] != brief["selected_opener"]["id"])
    r2 = client.get(f"/api/runs/{run_id}/swap/stream", params={"opener_id": other["id"]})
    evs2 = parse_sse(r2.text)
    done = evs2[-1]
    assert done["type"] == "done"
    assert done["brief"]["selected_opener"]["id"] == other["id"]
    assert other["name"] in done["brief"]["pitch_md"]
    assert done["brief"]["overlap"]["opener"] == other["name"]

    md = client.get(f"/api/runs/{run_id}/markdown")
    assert md.status_code == 200 and md.text.startswith("# Booking brief: Mitski")
    assert "attachment" in md.headers["content-disposition"]


def test_unknown_city_still_completes():
    b = client.post("/api/brief", json={"headliner": "Khruangbin", "city": "Reykjavik"}).json()
    assert b["heatmap"]["named"] is False
    assert b["heatmap"]["neighborhoods"][0]["name"].startswith("Area near")
    assert b["pitch_md"]


def test_input_validation():
    assert client.post("/api/brief", json={"headliner": "", "city": "Chicago"}).status_code == 422
    assert client.post("/api/brief", json={"headliner": "<script>", "city": "Chicago"}).status_code == 422
    assert client.post("/api/brief", json={"headliner": "X", "city": "Chicago", "show_date": "soon"}).status_code == 422
    assert client.get("/api/runs/nope").status_code == 404
