"""Unit tests: param validation, live-client behaviour (via httpx.MockTransport),
rate limiting, LLM tool loop + fallback, mock shapes."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app import mock_qloo
from app.agent import Agent
from app.config import Settings
from app.geo import geohash_decode, geohash_encode
from app.qloo_client import QlooCallFailed, QlooClient, QlooError, validate_insights_params
from app.ratelimit import RateLimiter


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_validation_strips_params_not_valid_for_type():
    clean, stripped = validate_insights_params({"filter.type": "urn:entity:artist", "signal.interests.entities": "A",
                                                "filter.location.query": "Chicago", "filter.price_level.min": 2})
    assert "filter.location.query" in stripped and "filter.price_level.min" in stripped
    assert clean == {"filter.type": "urn:entity:artist", "signal.interests.entities": "A"}
    clean, stripped = validate_insights_params({"filter.type": "urn:heatmap", "filter.location.query": "NYC",
                                                "signal.interests.entities": "A"})
    assert not stripped
    with pytest.raises(QlooError):
        validate_insights_params({"filter.type": "urn:entity:music"})


def test_forbidden_legacy_endpoints():
    q = QlooClient(Settings(qloo_mock_env="1", cache_path=":memory:"))
    with pytest.raises(QlooError):
        run(q.get("/recommendations", {}))


def _live(handler, **kw):
    s = Settings(qloo_api_key="test-key", qloo_mock_env="0", cache_path=":memory:", qloo_max_retries=2, **kw)
    q = QlooClient(s)
    q._http = httpx.AsyncClient(base_url=s.qloo_base_url, headers={"X-Api-Key": s.qloo_api_key},
                                transport=httpx.MockTransport(handler))
    return q


def test_live_client_header_retry_and_cache(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        if len(seen) == 1:
            return httpx.Response(503, json={"message": "busy"})
        return httpx.Response(200, json={"success": True, "results": {"entities": [{"name": "x"}]}})

    q = _live(handler)
    body, rec = run(q.get("/v2/insights", {"filter.type": "urn:entity:brand", "signal.interests.entities": "A"}))
    assert rec.count == 1 and rec.mock is False and not rec.cached
    assert seen[-1].headers["x-api-key"] == "test-key"
    assert seen[-1].url.host == "hackathon.api.qloo.com" and seen[-1].method == "GET"
    assert seen[-1].url.params["filter.type"] == "urn:entity:brand"
    assert len(seen) == 2  # one retry
    _, rec2 = run(q.get("/v2/insights", {"filter.type": "urn:entity:brand", "signal.interests.entities": "A"}))
    assert rec2.cached and len(seen) == 2


async def _nosleep(*_a, **_k):
    return None


def test_live_client_auth_error_is_clear():
    q = _live(lambda req: httpx.Response(401, json={"message": "No API key found in request"}))
    with pytest.raises(QlooCallFailed) as e:
        run(q.get("/search", {"query": "x"}))
    assert e.value.err.category == "auth" and e.value.record.http_status == 401


def test_rate_limiter():
    rl = RateLimiter(2)
    assert rl.check("ip")[0] and rl.check("ip")[0]
    ok, retry = rl.check("ip")
    assert not ok and retry > 0
    assert rl.check("other")[0]


def test_geohash_roundtrip():
    gh = geohash_encode(41.8781, -87.6298, 7)
    lat, lng = geohash_decode(gh)
    assert abs(lat - 41.8781) < 0.01 and abs(lng + 87.6298) < 0.01


def test_mock_shapes_match_docs():
    d = mock_qloo.respond("/v2/insights", {"filter.type": "urn:demographics", "signal.interests.entities": "A"})
    assert set(d["results"]["demographics"][0]["query"]) == {"age", "gender"}
    h = mock_qloo.respond("/v2/insights", {"filter.type": "urn:heatmap", "filter.location.query": "Chicago",
                                           "signal.interests.entities": "A", "take": 5})
    row = h["results"]["heatmap"][0]
    assert set(row["location"]) == {"latitude", "longitude", "geohash"}
    assert set(row["query"]) == {"affinity", "affinity_rank", "popularity"}
    t = mock_qloo.respond("/v2/trending", {"filter.type": "urn:entity:artist", "signal.interests.entities": "A",
                                           "filter.start_date": "2026-01-01", "filter.end_date": "2026-03-01"})
    dates = [r["date"] for r in t["results"]["trending"]]
    assert dates == sorted(dates, reverse=True)
    assert {"population_percentile", "population_rank", "population_rank_velocity", "velocity_fold_change",
            "population_percent_delta"} <= set(t["results"]["trending"][0])


# ---------- LLM agent loop with a fake OpenAI-compatible client ----------

class _FakeLLM:
    """Plays the role of a tool-calling model: calls each tool once, then finish."""
    def __init__(self, fail=False):
        self.fail = fail
        self.plan = [("resolve_artist", {"name": "Japanese Breakfast"}), ("find_openers", {"city": "Chicago"}),
                     ("audience_profile", {}), ("audience_heatmap", {}), ("find_venues", {"capacity": 300}),
                     ("find_sponsor_brands", {}), ("opener_momentum", {}), ("explain_overlap", {}),
                     ("finish", {"recommendation": "Book the top opener."})]
        self.i = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kw):
        if self.fail:
            raise RuntimeError("LLM down")
        if "tools" not in kw:  # pitch / llm-alone calls
            return _resp(content="**Pitch** from fake model citing 92% affinity.")
        name, args = self.plan[self.i]
        self.i += 1
        call = SimpleNamespace(id=f"c{self.i}", function=SimpleNamespace(name=name, arguments=json.dumps(args)))
        return _resp(content=f"Next: {name}", tool_calls=[call])


def _resp(content=None, tool_calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))])


def _agent(fake):
    s = Settings(qloo_mock_env="1", cache_path=":memory:", llm_api_key="x")
    a = Agent(s, QlooClient(s))
    a._llm = lambda: fake
    return a


def test_llm_tool_loop_drives_all_steps():
    events = []

    async def emit(ev):
        events.append(ev)

    fake = _FakeLLM()
    r = run(_agent(fake).run("Japanese Breakfast", "Chicago", 300, None, emit))
    st = r.state
    assert st.planner.startswith("llm:") and "deterministic" not in st.planner
    assert len(st.done_steps) == 8
    assert r.recommendation == "Book the top opener."
    assert st.pitch_source.startswith("llm:") and "fake model" in st.pitch_md
    assert any(e["type"] == "agent_thought" and "Next:" in e["text"] for e in events)


def test_llm_failure_falls_back_to_deterministic():
    async def emit(ev):
        pass

    r = run(_agent(_FakeLLM(fail=True)).run("Japanese Breakfast", "Chicago", None, None, emit))
    assert "deterministic" in r.state.planner
    assert len(r.state.done_steps) == 8
    assert r.state.pitch_source == "template" and r.state.pitch_md
