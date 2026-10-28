"""FastAPI app: serves the single-page UI and streams the agent trace over SSE."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import Agent, brief_dict
from .config import get_settings
from .pitch import brief_markdown
from .qloo_client import QlooClient
from .ratelimit import RateLimiter

ROOT = Path(__file__).resolve().parent.parent
settings = get_settings()
qloo = QlooClient(settings)
agent = Agent(settings, qloo)
limiter = RateLimiter(settings.rate_limit_per_hour)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await qloo.aclose()


app = FastAPI(title="Opening Act", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

_SAFE = re.compile(r"^[\w\s.,'&!?:()\-+/]+$", re.UNICODE)


class BriefRequest(BaseModel):
    headliner: str = Field(min_length=1, max_length=80)
    city: str = Field(min_length=1, max_length=60)
    capacity: int | None = Field(default=None, ge=20, le=100000)
    show_date: str | None = None


def _clean(req: BriefRequest) -> BriefRequest:
    for f in ("headliner", "city"):
        v = getattr(req, f).strip()
        if not v or not _SAFE.match(v):
            raise HTTPException(422, f"Invalid {f}")
        setattr(req, f, v)
    if req.show_date:
        try:
            dt.date.fromisoformat(req.show_date)
        except ValueError:
            raise HTTPException(422, "show_date must be YYYY-MM-DD")
    return req


def _client_ip(request: Request) -> str:
    if settings.trust_proxy:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limit(request: Request):
    ok, retry = limiter.check(_client_ip(request))
    if not ok:
        raise HTTPException(429, f"Demo rate limit reached. Try again in {retry // 60 + 1} min.",
                            headers={"Retry-After": str(retry)})


def _sse(job) -> StreamingResponse:
    queue: asyncio.Queue = asyncio.Queue()

    async def emit(ev: dict[str, Any]):
        await queue.put(ev)

    async def runner():
        try:
            await job(emit)
        except Exception as e:  # never leave the client hanging
            await queue.put({"type": "error", "error": f"{type(e).__name__}: {e}"[:300]})
        finally:
            await queue.put(None)

    async def gen() -> AsyncIterator[bytes]:
        task = asyncio.create_task(runner())
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield b": keep-alive\n\n"
                    continue
                if ev is None:
                    break
                yield f"event: {ev.get('type', 'message')}\ndata: {json.dumps(ev, default=str)}\n\n".encode()
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/")
async def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/health")
async def health():
    return {"ok": True, "mock": qloo.mock, "llm": settings.llm_enabled,
            "model": settings.llm_model if settings.llm_enabled else None,
            "qloo_base_url": settings.qloo_base_url}


@app.get("/api/brief/stream")
async def brief_stream(request: Request, headliner: str, city: str, capacity: int | None = None,
                       show_date: str | None = None):
    try:
        req = _clean(BriefRequest(headliner=headliner, city=city, capacity=capacity, show_date=show_date or None))
    except ValueError as e:
        raise HTTPException(422, str(e)[:200])
    _rate_limit(request)
    return _sse(lambda emit: agent.run(req.headliner, req.city, req.capacity, req.show_date, emit))


@app.post("/api/brief")
async def brief(request: Request, req: BriefRequest):
    req = _clean(req)
    _rate_limit(request)
    events: list[dict] = []

    async def emit(ev):
        events.append(ev)

    run = await agent.run(req.headliner, req.city, req.capacity, req.show_date, emit)
    return brief_dict(run) | {"trace": [e for e in events if e["type"] != "done"]}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    run = agent.runs.get(run_id)
    if not run:
        raise HTTPException(404, "Run not found (runs are kept in memory).")
    return brief_dict(run)


@app.get("/api/runs/{run_id}/markdown")
async def run_markdown(run_id: str):
    run = agent.runs.get(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    b = brief_dict(run)
    slug = re.sub(r"[^a-z0-9]+", "-", f"{(b.get('headliner') or {}).get('name', 'brief')}-{b['inputs']['city']}".lower())
    return PlainTextResponse(brief_markdown(b), media_type="text/markdown",
                             headers={"Content-Disposition": f'attachment; filename="opening-act-{slug.strip("-")}.md"'})


@app.get("/api/runs/{run_id}/swap/stream")
async def swap_stream(request: Request, run_id: str, opener_id: str):
    run = agent.runs.get(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    if not any(o["id"] == opener_id for o in run.state.openers):
        raise HTTPException(404, "Opener not in this run")
    _rate_limit(request)
    return _sse(lambda emit: agent.swap_opener(run_id, opener_id, emit))


@app.exception_handler(HTTPException)
async def http_exc(_request: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=exc.headers)

