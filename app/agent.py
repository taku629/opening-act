"""Opening Act agent: an OpenAI-compatible tool-calling loop over Qloo tools, with a
deterministic planner fallback so the demo never breaks, then a pitch writer."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

from .config import Settings
from .pitch import llm_alone_answer, template_pitch, write_pitch
from .qloo_client import QlooCallFailed, QlooClient
from .tools import Emit, RunState, ToolError, Toolbox

STEPS = [
    ("resolve", "Resolve headliner", "GET /search"),
    ("openers", "Find audience-overlap openers", "GET /v2/insights · urn:entity:artist"),
    ("demographics", "Profile the audience", "GET /v2/insights · urn:demographics"),
    ("heatmap", "Map where fans live", "GET /v2/insights · urn:heatmap"),
    ("venues", "Match venues", "GET /v2/tags → /v2/insights · urn:entity:place"),
    ("brands", "Find sponsor brands", "GET /v2/insights · urn:entity:brand"),
    ("momentum", "Check opener momentum", "GET /v2/trending"),
    ("overlap", "Explain the overlap", "GET /v2/analysis/compare + urn:tag"),
]
STEP_TITLES = {s: t for s, t, _ in STEPS}

TOOL_SPECS = [
    {"type": "function", "function": {"name": "resolve_artist", "description":
        "Resolve the headliner's name to a Qloo artist entity (GET /search). Always call first.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {"name": "find_openers", "description":
        "Artists whose audiences overlap with the headliner (Qloo insights, artist type), weighted to the city, "
        "ranked to favour smaller emerging acts.",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"},
                                                         "emerging_only": {"type": "boolean"}}}}},
    {"type": "function", "function": {"name": "audience_profile", "description":
        "Age and gender affinity of the headliner's audience (Qloo urn:demographics).",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "audience_heatmap", "description":
        "Where in the city the headliner's audience concentrates (Qloo urn:heatmap) -> top neighborhoods.",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "find_venues", "description":
        "Music venues / bars in the city whose patrons share the headliner's taste (Qloo tags + place insights).",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"},
                                                         "capacity": {"type": "integer"}}}}},
    {"type": "function", "function": {"name": "find_sponsor_brands", "description":
        "Brands with high affinity to the headliner's audience (Qloo brand insights) for sponsorship.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "opener_momentum", "description":
        "12-week popularity trend for up to 3 candidate openers (Qloo /v2/trending).",
        "parameters": {"type": "object", "properties": {"opener_ids": {"type": "array", "items": {"type": "string"}}}}}},
    {"type": "function", "function": {"name": "explain_overlap", "description":
        "Explain why an opener fits: shared taste tags between headliner and opener "
        "(Qloo /v2/analysis/compare and urn:tag taste analysis). Pick the best opener considering momentum.",
        "parameters": {"type": "object", "properties": {"opener_id": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "finish", "description":
        "Call when the brief has everything it needs. Give a one-sentence booking recommendation.",
        "parameters": {"type": "object", "properties": {"recommendation": {"type": "string"}},
                       "required": ["recommendation"]}}},
]
TOOL_TO_STEP = {"resolve_artist": "resolve", "find_openers": "openers", "audience_profile": "demographics",
                "audience_heatmap": "heatmap", "find_venues": "venues", "find_sponsor_brands": "brands",
                "opener_momentum": "momentum", "explain_overlap": "overlap"}

SYSTEM = """You are Opening Act, a booking analyst for independent promoters and small-venue bookers.
You plan a show around a headliner using ONLY numbers returned by the Qloo taste-graph tools.
Work step by step: resolve the headliner, find audience-overlap openers, profile the audience,
map where fans live in the city, match venues, find sponsor brands, check momentum of the top openers,
then explain the overlap for the opener you would book (prefer high affinity + emerging + rising).
Call one tool at a time. Before each tool call write one short sentence of reasoning.
Never invent artists, venues, brands or numbers. When done, call finish."""


class Run:
    def __init__(self, state: RunState):
        self.id = uuid.uuid4().hex[:12]
        self.state = state
        self.created = time.time()
        self.recommendation = ""


class Agent:
    def __init__(self, settings: Settings, qloo: QlooClient):
        self.s = settings
        self.q = qloo
        self.runs: dict[str, Run] = {}

    def _llm(self):
        if not self.s.llm_enabled:
            return None
        from openai import AsyncOpenAI
        return AsyncOpenAI(base_url=self.s.llm_base_url, api_key=self.s.llm_api_key, timeout=self.s.llm_timeout_s,
                           max_retries=1)

    def _remember(self, run: Run):
        self.runs[run.id] = run
        if len(self.runs) > 200:
            for k in sorted(self.runs, key=lambda k: self.runs[k].created)[:50]:
                self.runs.pop(k, None)

    async def _exec_tool(self, tb: Toolbox, name: str, args: dict, emit: Emit) -> dict:
        step = TOOL_TO_STEP.get(name)
        if not step:
            return {"error": f"unknown tool {name}"}
        await emit({"type": "step_start", "step": step, "title": STEP_TITLES[step], "args": args})
        fn = getattr(tb, name)
        try:
            result = await fn(**{k: v for k, v in args.items() if v not in (None, "")})
        except (ToolError, QlooCallFailed, TypeError) as e:
            msg = e.err.message if isinstance(e, QlooCallFailed) else str(e)
            await emit({"type": "step_error", "step": step, "title": STEP_TITLES[step], "error": msg})
            return {"error": msg}
        if step not in tb.st.done_steps:
            tb.st.done_steps.append(step)
        await emit({"type": "step_done", "step": step, "title": STEP_TITLES[step], "data": step_payload(tb.st, step)})
        return result

    async def _deterministic(self, tb: Toolbox, emit: Emit, only_missing: bool = False):
        st = tb.st
        plan = [("resolve_artist", {"name": st.inputs["headliner"]}), ("find_openers", {}),
                ("audience_profile", {}), ("audience_heatmap", {}), ("find_venues", {}),
                ("find_sponsor_brands", {}), ("opener_momentum", {}), ("explain_overlap", {})]
        for name, args in plan:
            step = TOOL_TO_STEP[name]
            if only_missing and step in st.done_steps:
                continue
            res = await self._exec_tool(tb, name, args, emit)
            if name == "resolve_artist" and "error" in res:
                return
            if name == "opener_momentum" and "error" not in res:
                pick_best_opener(st)

    async def _llm_loop(self, tb: Toolbox, emit: Emit, client) -> bool:
        st = tb.st
        inp = st.inputs
        user = (f"Headliner: {inp['headliner']}\nCity: {inp['city']}\n"
                f"Venue capacity: {inp.get('capacity') or 'not specified'}\nShow date: {inp.get('show_date') or 'not set'}")
        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
        for _ in range(14):
            resp = await client.chat.completions.create(model=self.s.llm_model, messages=messages,
                                                        tools=TOOL_SPECS, tool_choice="auto", temperature=0.2,
                                                        max_tokens=600)
            msg = resp.choices[0].message
            calls = msg.tool_calls or []
            if msg.content and msg.content.strip():
                await emit({"type": "agent_thought", "text": msg.content.strip()[:500]})
            if not calls:
                return bool(st.done_steps)
            messages.append({"role": "assistant", "content": msg.content or "",
                             "tool_calls": [{"id": c.id, "type": "function",
                                             "function": {"name": c.function.name,
                                                          "arguments": c.function.arguments or "{}"}}
                                            for c in calls]})
            finished = False
            for c in calls:
                try:
                    args = json.loads(c.function.arguments or "{}") or {}
                except json.JSONDecodeError:
                    args = {}
                if c.function.name == "finish":
                    finished = True
                    result = {"ok": True}
                    st.recommendation = str(args.get("recommendation", ""))[:400]
                else:
                    result = await self._exec_tool(tb, c.function.name, args, emit)
                messages.append({"role": "tool", "tool_call_id": c.id,
                                 "content": json.dumps(result, default=str)[:3500]})
            if finished:
                return True
        return True

    async def run(self, headliner: str, city: str, capacity: int | None, show_date: str | None, emit: Emit) -> Run:
        st = RunState(headliner, city, capacity, show_date, self.q.mock)
        run = Run(st)
        self._remember(run)
        await emit({"type": "run_start", "run_id": run.id, "inputs": st.inputs, "mock": self.q.mock,
                    "llm": self.s.llm_enabled, "model": self.s.llm_model if self.s.llm_enabled else None,
                    "steps": [{"step": s, "title": t, "endpoint": e} for s, t, e in STEPS]})
        tb = Toolbox(self.q, st, emit)
        client = self._llm()
        if client is not None:
            st.planner = f"llm:{self.s.llm_model}"
            try:
                await self._llm_loop(tb, emit, client)
            except Exception as e:  # LLM outage, bad tool-call support, timeouts ...
                st.notes.append(f"LLM planner failed ({type(e).__name__}); deterministic planner finished the run.")
                await emit({"type": "agent_thought", "text": "LLM planner unavailable - switching to deterministic planner."})
                st.planner += " → deterministic"
        if st.headliner is None or len(st.done_steps) < len(STEPS):
            if client is None:
                await emit({"type": "agent_thought", "text": "No LLM configured - running the deterministic planner."})
            await self._deterministic(tb, emit, only_missing=True)
        run.recommendation = st.recommendation
        if st.headliner:
            await self.write_pitch(run, emit, client)
        await emit({"type": "done", "run_id": run.id, "brief": brief_dict(run)})
        return run

    async def write_pitch(self, run: Run, emit: Emit, client=None):
        st = run.state
        client = client if client is not None else self._llm()
        await emit({"type": "step_start", "step": "pitch", "title": "Write booking pitch"})
        pitch_task = write_pitch(client, self.s.llm_model, st, run.recommendation)
        alone_task = llm_alone_answer(client, self.s.llm_model, st)
        pitch, alone = await asyncio.gather(pitch_task, alone_task, return_exceptions=True)
        if isinstance(pitch, Exception) or not pitch:
            st.pitch_md, st.pitch_source = template_pitch(st), "template"
        else:
            st.pitch_md, st.pitch_source = pitch, f"llm:{self.s.llm_model}"
        st.llm_alone = alone if isinstance(alone, str) and alone else ""
        await emit({"type": "pitch", "markdown": st.pitch_md, "source": st.pitch_source, "llm_alone": st.llm_alone})

    async def swap_opener(self, run_id: str, opener_id: str, emit: Emit) -> Run:
        run = self.runs.get(run_id)
        if not run:
            raise KeyError(run_id)
        st = run.state
        opener = next((o for o in st.openers if o["id"] == opener_id), None)
        if not opener:
            raise KeyError(opener_id)
        st.selected_opener = opener
        tb = Toolbox(self.q, st, emit)
        if opener_id not in st.momentum:
            await self._exec_tool(tb, "opener_momentum", {"opener_ids": [opener_id]}, emit)
        await self._exec_tool(tb, "explain_overlap", {"opener_id": opener_id}, emit)
        run.recommendation = ""
        await self.write_pitch(run, emit)
        await emit({"type": "done", "run_id": run.id, "brief": brief_dict(run)})
        return run


def pick_best_opener(st: RunState):
    """Prefer high opener score, boosted when Qloo trending says the act is rising."""
    if not st.openers:
        return
    def k(o):
        m = st.momentum.get(o["id"]) or {}
        boost = {"rising": 0.06, "steady": 0.0, "cooling": -0.05}.get(m.get("label"), 0)
        return o["score"] + boost
    st.selected_opener = max(st.openers[:3], key=k)


def step_payload(st: RunState, step: str) -> Any:
    return {
        "resolve": st.headliner, "openers": st.openers, "demographics": st.demographics,
        "heatmap": st.heatmap, "venues": st.venues, "brands": st.brands, "momentum": st.momentum,
        "overlap": st.overlap,
    }.get(step)


def brief_dict(run: Run) -> dict:
    st = run.state
    return {"run_id": run.id, "inputs": st.inputs, "mock": st.mock, "planner": st.planner,
            "headliner": st.headliner, "openers": st.openers, "selected_opener": st.selected_opener,
            "demographics": st.demographics, "heatmap": st.heatmap, "venues": st.venues, "brands": st.brands,
            "momentum": st.momentum, "overlap": st.overlap, "pitch_md": st.pitch_md,
            "pitch_source": st.pitch_source, "llm_alone": st.llm_alone, "notes": st.notes,
            "recommendation": run.recommendation,
            "calls": st.calls, "qloo_calls": len(st.calls)}
