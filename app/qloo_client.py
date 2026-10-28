"""Qloo API client.

* Only the hackathon base URL, `X-Api-Key` header, GET + query params.
* Validates /v2/insights params against the Entity Type Parameter Guide, because the
  API silently ignores invalid params (we strip + report them instead).
* Timeouts, bounded retries with backoff on 429/5xx/network errors, clear error
  categories, in-memory + SQLite response cache (private, server-side only).
* When no QLOO_API_KEY is set (or QLOO_MOCK=1) every call is answered by
  `mock_qloo.respond`, which synthesises documented response shapes.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import mock_qloo
from .config import Settings

FORBIDDEN_PATHS = ("/recs", "/recommendations")

_COMMON_ENTITY = {
    "filter.type", "bias.trends", "filter.exclude.entities", "filter.parents.types",
    "filter.popularity.min", "filter.popularity.max", "filter.exclude.tags", "operator.exclude.tags",
    "filter.external.exists", "operator.filter.external.exists", "filter.results.entities",
    "filter.results.entities.query", "filter.tags", "operator.filter.tags", "offset",
    "signal.demographics.age", "signal.demographics.audiences", "signal.demographics.audiences.weight",
    "signal.demographics.gender", "signal.interests.entities", "signal.interests.tags", "take", "page",
}
# Location *signals* are documented as generally applicable in the Location-Based
# Insights guide (its example uses them with urn:entity:movie), so we allow them on
# entity types even though the per-type tables do not list them.
_LOCATION_SIGNAL = {"signal.location", "signal.location.query", "signal.location.radius", "signal.location.weight"}

ALLOWED_INSIGHTS_PARAMS: dict[str, set[str]] = {
    "urn:entity:artist": _COMMON_ENTITY | _LOCATION_SIGNAL,
    "urn:entity:brand": _COMMON_ENTITY | _LOCATION_SIGNAL,
    "urn:entity:place": _COMMON_ENTITY | _LOCATION_SIGNAL | {
        "bias.quality", "filter.address", "filter.external.tripadvisor.rating.count.max",
        "filter.external.tripadvisor.rating.count.min", "filter.external.tripadvisor.rating.max",
        "filter.external.tripadvisor.rating.min", "filter.geocode.name", "filter.geocode.admin1_region",
        "filter.geocode.admin2_region", "filter.geocode.country_code", "filter.hours", "filter.location",
        "filter.location.query", "filter.location.geohash", "filter.exclude.location.geohash",
        "filter.location.radius", "filter.price_level.min", "filter.price_level.max",
        "filter.properties.business_rating.min", "filter.properties.business_rating.max",
        "filter.references_brand", "sort_by",
    },
    "urn:heatmap": {
        "filter.type", "filter.location", "filter.location.query", "filter.location.radius",
        "heatmap.dimensions", "filter.dimensions.hotel_class", "filter.dimensions.price_level",
        "output.heatmap.boundary", "signal.demographics.age", "signal.demographics.audiences",
        "signal.demographics.gender", "signal.interests.entities", "signal.interests.tags", "take",
        "bias.trends", "signal.demographics.audiences.weight",
    },
    # Demographic Insights guide: filter.type + one of the two interest signals.
    "urn:demographics": {"filter.type", "signal.interests.entities", "signal.interests.tags"},
    # Taste Analysis guide.
    "urn:tag": {
        "filter.type", "diversify.by", "diversify.take", "offset", "take", "page",
        "signal.interests.entities", "signal.interests.tags", "signal.demographics.audiences",
        "signal.location", "signal.location.query", "filter.tag.types", "filter.parents.types",
    },
}


class QlooError(Exception):
    def __init__(self, category: str, message: str, status: int | None = None):
        super().__init__(f"[{category}] {message}")
        self.category = category
        self.message = message
        self.status = status


@dataclass
class CallRecord:
    endpoint: str
    params: dict[str, Any]
    status: str = "ok"           # ok | error
    http_status: int | None = None
    count: int = 0
    duration_ms: int = 0
    cached: bool = False
    mock: bool = False
    stripped_params: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def validate_insights_params(params: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    ftype = params.get("filter.type")
    allowed = ALLOWED_INSIGHTS_PARAMS.get(str(ftype))
    if allowed is None:
        raise QlooError("invalid_request", f"Unsupported filter.type for this app: {ftype}")
    clean, stripped = {}, []
    for k, v in params.items():
        if v is None or v == "":
            continue
        if k in allowed:
            clean[k] = v
        else:
            stripped.append(k)
    return clean, stripped


def count_results(body: Any) -> int:
    if not isinstance(body, dict):
        return 0
    res = body.get("results")
    if isinstance(res, list):
        return len(res)
    if isinstance(res, dict):
        for key in ("entities", "tags", "heatmap", "demographics", "trending"):
            if isinstance(res.get(key), list):
                return len(res[key])
    return 0


class _Cache:
    def __init__(self, path: str, ttl_s: int):
        self.ttl = ttl_s
        self.mem: dict[str, tuple[float, Any]] = {}
        self.lock = threading.Lock()
        self.db = None
        try:
            self.db = sqlite3.connect(path, check_same_thread=False)
            self.db.execute("CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, t REAL, v TEXT)")
            self.db.commit()
        except Exception:  # pragma: no cover - cache is best effort
            self.db = None

    def get(self, key: str) -> Any | None:
        now = time.time()
        with self.lock:
            hit = self.mem.get(key)
            if hit and now - hit[0] < self.ttl:
                return hit[1]
            if self.db is not None:
                row = self.db.execute("SELECT t, v FROM cache WHERE k=?", (key,)).fetchone()
                if row and now - row[0] < self.ttl:
                    val = json.loads(row[1])
                    self.mem[key] = (row[0], val)
                    return val
        return None

    def set(self, key: str, val: Any) -> None:
        now = time.time()
        with self.lock:
            self.mem[key] = (now, val)
            if len(self.mem) > 2000:
                for k in list(self.mem)[:500]:
                    self.mem.pop(k, None)
            if self.db is not None:
                try:
                    self.db.execute("REPLACE INTO cache (k, t, v) VALUES (?,?,?)", (key, now, json.dumps(val)))
                    self.db.commit()
                except Exception:  # pragma: no cover
                    pass


class QlooClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self.mock = settings.qloo_mock
        self.cache = _Cache(settings.cache_path, settings.cache_ttl_s)
        self._http: httpx.AsyncClient | None = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self.s.qloo_base_url,
                headers={"X-Api-Key": self.s.qloo_api_key, "Accept": "application/json"},
                timeout=self.s.qloo_timeout_s,
            )
        return self._http

    async def aclose(self):
        if self._http is not None:
            await self._http.aclose()

    async def get(self, path: str, params: dict[str, Any]) -> tuple[dict[str, Any], CallRecord]:
        if any(path.startswith(p) for p in FORBIDDEN_PATHS):
            raise QlooError("invalid_request", "Legacy /recs and /recommendations endpoints are not allowed")
        stripped: list[str] = []
        if path == "/v2/insights":
            params, stripped = validate_insights_params(params)
        params = {k: (",".join(map(str, v)) if isinstance(v, (list, tuple)) else v) for k, v in params.items()}
        rec = CallRecord(endpoint=path, params=dict(params), mock=self.mock, stripped_params=stripped)
        key = hashlib.sha256(json.dumps([self.mock, path, sorted(params.items())], default=str).encode()).hexdigest()
        t0 = time.perf_counter()

        cached = self.cache.get(key)
        if cached is not None:
            rec.cached, rec.count = True, count_results(cached)
            rec.http_status = 200
            rec.duration_ms = int((time.perf_counter() - t0) * 1000)
            return cached, rec

        try:
            if self.mock:
                await asyncio.sleep(0.12)  # keep the visible trace honest about latency
                body = mock_qloo.respond(path, params)
                rec.http_status = 200
            else:
                body, rec.http_status = await self._live_get(path, params)
        except QlooError as e:
            rec.status, rec.error, rec.http_status = "error", e.message, e.status
            rec.duration_ms = int((time.perf_counter() - t0) * 1000)
            raise QlooCallFailed(e, rec) from e

        rec.count = count_results(body)
        rec.duration_ms = int((time.perf_counter() - t0) * 1000)
        self.cache.set(key, body)
        return body, rec

    async def _live_get(self, path: str, params: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if not self.s.qloo_api_key:
            raise QlooError("auth", "QLOO_API_KEY is not set")
        client = await self._client()
        attempt, last_exc = 0, None
        while attempt <= self.s.qloo_max_retries:
            try:
                r = await client.get(path, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_exc = QlooError("network", f"{type(e).__name__}: {e}")
            else:
                if r.status_code == 200:
                    try:
                        return r.json(), 200
                    except ValueError:
                        raise QlooError("bad_response", "Qloo returned non-JSON", 200)
                msg = _error_message(r)
                if r.status_code == 401:
                    raise QlooError("auth", f"401 Unauthorized - check QLOO_API_KEY and base URL. {msg}", 401)
                if r.status_code == 403:
                    raise QlooError("forbidden", f"403 - entity type or endpoint not permitted. {msg}", 403)
                if r.status_code in (400, 404, 422):
                    raise QlooError("invalid_request" if r.status_code != 404 else "not_found", msg, r.status_code)
                if r.status_code == 429:
                    last_exc = QlooError("rate_limited", f"429 rate limited. {msg}", 429)
                    retry_after = r.headers.get("retry-after")
                    if retry_after and retry_after.isdigit() and int(retry_after) > 10:
                        raise last_exc
                else:
                    last_exc = QlooError("server", f"{r.status_code} from Qloo. {msg}", r.status_code)
            attempt += 1
            if attempt <= self.s.qloo_max_retries:
                await asyncio.sleep(0.6 * (2 ** (attempt - 1)))
        assert last_exc is not None
        raise last_exc


class QlooCallFailed(Exception):
    def __init__(self, err: QlooError, record: CallRecord):
        super().__init__(str(err))
        self.err = err
        self.record = record


def _error_message(r: httpx.Response) -> str:
    try:
        j = r.json()
        if isinstance(j, dict):
            for k in ("message", "error", "reason", "errors"):
                if j.get(k):
                    return str(j[k])[:300]
    except ValueError:
        pass
    return (r.text or "")[:300]
