"""The agent's tools. Each tool wraps one or more Qloo calls, stores structured
results on the run state (for the UI cards) and returns a compact summary (for the
LLM's context). All parsing is defensive: unknown fields are ignored, missing
fields degrade gracefully."""
from __future__ import annotations

import datetime as dt
import statistics
from typing import Any, Awaitable, Callable

from .geo import city_info, geohash_decode, nearest_neighborhood
from .qloo_client import QlooCallFailed, QlooClient

Emit = Callable[[dict[str, Any]], Awaitable[None]]


class RunState:
    def __init__(self, headliner: str, city: str, capacity: int | None, show_date: str | None, mock: bool):
        self.inputs = {"headliner": headliner, "city": city, "capacity": capacity, "show_date": show_date}
        self.mock = mock
        self.headliner: dict | None = None
        self.openers: list[dict] = []
        self.selected_opener: dict | None = None
        self.demographics: dict | None = None
        self.heatmap: dict | None = None
        self.venues: list[dict] = []
        self.venue_tags: list[dict] = []
        self.brands: list[dict] = []
        self.momentum: dict[str, dict] = {}
        self.overlap: dict | None = None
        self.calls: list[dict] = []
        self.notes: list[str] = []
        self.done_steps: list[str] = []
        self.pitch_md: str = ""
        self.pitch_source: str = ""
        self.llm_alone: str = ""
        self.planner: str = "deterministic"
        self.recommendation: str = ""


class ToolError(Exception):
    pass


# ---------- helpers ----------

def _num(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _entities(body: dict) -> list[dict]:
    res = body.get("results")
    if isinstance(res, dict):
        return [e for e in res.get("entities", []) if isinstance(e, dict)]
    if isinstance(res, list):
        return [e for e in res if isinstance(e, dict)]
    return []


def _affinity(e: dict) -> float | None:
    q = e.get("query") or {}
    return _num(q.get("affinity"), _num(q.get("affinity_score")))


def _tags(e: dict, limit: int = 4) -> list[str]:
    out = []
    for t in e.get("tags") or []:
        if isinstance(t, dict) and t.get("name"):
            ttype = str(t.get("type") or t.get("subtype") or "")
            if "genre" in ttype or "keyword" in ttype or not ttype:
                out.append(t["name"])
    return out[:limit]


def _image(e: dict) -> str | None:
    img = (e.get("properties") or {}).get("image")
    if isinstance(img, dict):
        return img.get("url")
    return img if isinstance(img, str) else None


class Toolbox:
    def __init__(self, qloo: QlooClient, state: RunState, emit: Emit):
        self.q = qloo
        self.st = state
        self.emit = emit

    async def call(self, step: str, path: str, params: dict) -> dict:
        try:
            body, rec = await self.q.get(path, params)
        except QlooCallFailed as f:
            d = f.record.to_dict() | {"step": step, "category": f.err.category}
            self.st.calls.append(d)
            await self.emit({"type": "qloo_call", **d})
            raise
        d = rec.to_dict() | {"step": step}
        self.st.calls.append(d)
        await self.emit({"type": "qloo_call", **d})
        return body

    async def insights_with_location(self, step: str, params: dict, loc_key: str, city: str,
                                     fallback_key: str | None = None) -> dict:
        """Try a location-scoped insights call; if Qloo cannot resolve the locality
        (documented 400) fall back to the alternative key, then to no location."""
        attempts = [loc_key] + ([fallback_key] if fallback_key else []) + [None]
        last: Exception | None = None
        for key in attempts:
            p = dict(params)
            if key:
                p[key] = city
            try:
                body = await self.call(step, "/v2/insights", p)
                if key is None and city:
                    self.st.notes.append(f"{step}: location '{city}' could not be applied; results are not city-scoped.")
                return body
            except QlooCallFailed as f:
                last = f
                if f.err.category not in ("invalid_request", "not_found"):
                    raise
        assert last is not None
        raise last

    def need_headliner(self) -> dict:
        if not self.st.headliner:
            raise ToolError("Resolve the headliner first (resolve_artist).")
        return self.st.headliner

    # ---------- tools ----------

    async def resolve_artist(self, name: str) -> dict:
        body = await self.call("resolve", "/search", {"query": name, "types": "urn:entity:artist", "take": 5})
        ents = _entities(body)
        if not ents:
            raise ToolError(f"No artist found for '{name}'.")
        exact = [e for e in ents if str(e.get("name", "")).lower() == name.strip().lower()]
        e = exact[0] if exact else ents[0]
        self.st.headliner = {
            "id": e.get("entity_id") or e.get("id"), "name": e.get("name", name),
            "popularity": _num(e.get("popularity"), 0.0), "tags": _tags(e), "image": _image(e),
            "disambiguation": e.get("disambiguation"),
            "alternatives": [x.get("name") for x in ents if x is not e][:3],
        }
        return {"headliner": {k: self.st.headliner[k] for k in ("id", "name", "popularity", "tags")}}

    async def find_openers(self, city: str | None = None, emerging_only: bool = True, take: int = 30) -> dict:
        h = self.need_headliner()
        city = city or self.st.inputs["city"]
        params = {
            "filter.type": "urn:entity:artist",
            "signal.interests.entities": h["id"],
            "filter.exclude.entities": h["id"],
            "take": max(5, min(int(take), 50)),
        }
        hp = h.get("popularity") or 0
        if emerging_only and hp > 0.3:
            # nobody bigger than the headliner; the ranking below favours smaller acts
            params["filter.popularity.max"] = round(min(hp, 0.99), 4)
        body = await self.insights_with_location("openers", params, "signal.location.query", city)
        rows = []
        for e in _entities(body):
            aff = _affinity(e) or 0.0
            pop = _num(e.get("popularity"), 0.0)
            growth = _num(((e.get("query") or {}).get("measurements") or {}).get("audience_growth"))
            rel = pop / hp if hp else pop
            # Opener score: shared-audience affinity, discounted for acts as big as the headliner.
            score = aff * (1 - 0.55 * max(0.0, min(rel, 1.0)) ** 3)
            tier = "emerging" if rel < 0.8 else ("rising" if rel < 0.95 else "peer")
            rows.append({"id": e.get("entity_id"), "name": e.get("name"), "affinity": round(aff, 4),
                         "popularity": round(pop, 4), "audience_growth": growth, "tags": _tags(e, 3),
                         "image": _image(e), "score": round(score, 4), "tier": tier})
        rows.sort(key=lambda r: -r["score"])
        self.st.openers = rows[:12]
        if self.st.openers and not self.st.selected_opener:
            self.st.selected_opener = self.st.openers[0]
        return {"openers": [{k: r[k] for k in ("id", "name", "affinity", "popularity", "tier", "score")}
                            for r in self.st.openers[:8]]}

    async def audience_profile(self) -> dict:
        h = self.need_headliner()
        body = await self.call("demographics", "/v2/insights",
                               {"filter.type": "urn:demographics", "signal.interests.entities": h["id"]})
        demos = ((body.get("results") or {}).get("demographics")) or []
        row = next((d for d in demos if d.get("entity_id") == h["id"]), demos[0] if demos else None)
        if not row:
            raise ToolError("No demographic data returned for this artist.")
        q = row.get("query") or {}
        age = {k: _num(v, 0.0) for k, v in (q.get("age") or {}).items()}
        gender = {k: _num(v, 0.0) for k, v in (q.get("gender") or {}).items()}
        top_age = max(age, key=age.get) if age else None
        skew = None
        if gender:
            g = max(gender, key=gender.get)
            skew = {"leans": g, "score": gender[g]}
        self.st.demographics = {"age": age, "gender": gender, "top_age": top_age, "gender_skew": skew}
        return {"age_affinity": age, "gender_affinity": gender, "strongest_age_band": top_age}

    async def audience_heatmap(self, city: str | None = None) -> dict:
        h = self.need_headliner()
        city = city or self.st.inputs["city"]
        params = {"filter.type": "urn:heatmap", "filter.location.query": city,
                  "signal.interests.entities": h["id"], "take": 50}
        if self.q.s.heatmap_boundary:
            params["output.heatmap.boundary"] = self.q.s.heatmap_boundary
        body = await self.call("heatmap", "/v2/insights", params)
        rows = ((body.get("results") or {}).get("heatmap")) or []
        loc_meta = (((body.get("query") or {}).get("localities") or {}).get("filter")) or {}
        points, groups = [], {}
        for r in rows:
            loc, q = r.get("location") or {}, r.get("query") or {}
            lat, lng = _num(loc.get("latitude")), _num(loc.get("longitude"))
            if (lat is None or lng is None) and loc.get("geohash"):
                lat, lng = geohash_decode(loc["geohash"])
            if lat is None or lng is None:
                continue
            aff = _num(q.get("affinity"), 0.0)
            label = r.get("name") or nearest_neighborhood(city, lat, lng) or \
                f"Area near {lat:.3f}, {lng:.3f}"
            points.append({"lat": lat, "lng": lng, "affinity": aff, "popularity": _num(q.get("popularity")),
                           "label": label})
            groups.setdefault(label, []).append(aff)
        total = sum(sum(v) for v in groups.values()) or 1.0
        # "Heat" = summed affinity of the grid cells in an area: rewards dense clusters of fans,
        # not a single hot cell. share = that area's slice of all audience heat in the city.
        hoods = sorted(({"name": k, "affinity": round(max(v), 4), "mean_affinity": round(statistics.mean(v), 4),
                         "cells": len(v), "share": round(sum(v) / total, 4)} for k, v in groups.items()),
                       key=lambda x: (-x["share"], -x["affinity"]))
        info = city_info(city)
        self.st.heatmap = {"city": loc_meta.get("name") or (info[0] if info else city), "points": points,
                           "neighborhoods": hoods[:6], "named": bool(info)}
        return {"top_areas": hoods[:5], "cells": len(points)}

    async def find_venues(self, city: str | None = None, capacity: int | None = None) -> dict:
        h = self.need_headliner()
        city = city or self.st.inputs["city"]
        capacity = capacity if capacity is not None else self.st.inputs.get("capacity")
        tag_ids: list[str] = []
        for q in ("music venue", "bar"):
            body = await self.call("venues", "/v2/tags", {"filter.query": q, "take": 5})
            for t in ((body.get("results") or {}).get("tags")) or []:
                tid = t.get("id") or t.get("tag_id")
                name = str(t.get("name", "")).lower()
                if tid and ("venue" in name or "music" in name or name in {"bar", "concert hall"}):
                    if tid not in tag_ids:
                        tag_ids.append(tid)
                        self.st.venue_tags.append({"id": tid, "name": t.get("name")})
            if len(tag_ids) >= 3:
                break
        params = {"filter.type": "urn:entity:place", "signal.interests.entities": h["id"], "take": 12,
                  "filter.tags": ",".join(tag_ids[:4]) if tag_ids else None, "operator.filter.tags": "union"}
        if capacity and capacity <= 500:
            # Qloo has no capacity field; popularity percentile is used as a soft size proxy.
            params["filter.popularity.max"] = 0.9 if capacity <= 250 else 0.96
        body = await self.insights_with_location("venues", params, "filter.location.query", city,
                                                 fallback_key="signal.location.query")
        out = []
        for e in _entities(body):
            props = e.get("properties") or {}
            loc = e.get("location") or {}
            lat, lng = _num(loc.get("lat")), _num(loc.get("lon", loc.get("lng")))
            hood = props.get("neighborhood") or (nearest_neighborhood(city, lat, lng) if lat and lng else None)
            out.append({"id": e.get("entity_id"), "name": e.get("name"), "address": props.get("address"),
                        "neighborhood": hood, "affinity": round(_affinity(e) or 0, 4),
                        "popularity": _num(e.get("popularity")), "rating": _num(props.get("business_rating")),
                        "lat": lat, "lng": lng,
                        "tags": [t.get("name") for t in (e.get("tags") or []) if isinstance(t, dict)][:2]})
        self.st.venues = out[:8]
        return {"venues": [{k: v[k] for k in ("name", "neighborhood", "affinity")} for v in self.st.venues]}

    async def find_sponsor_brands(self) -> dict:
        h = self.need_headliner()
        body = await self.call("brands", "/v2/insights",
                               {"filter.type": "urn:entity:brand", "signal.interests.entities": h["id"], "take": 10})
        out = []
        for e in _entities(body):
            cats = [t.get("name") for t in (e.get("tags") or []) if isinstance(t, dict)][:2]
            out.append({"id": e.get("entity_id"), "name": e.get("name"), "affinity": round(_affinity(e) or 0, 4),
                        "popularity": _num(e.get("popularity")), "categories": cats})
        self.st.brands = out[:8]
        return {"brands": [{k: b[k] for k in ("name", "affinity", "categories")} for b in self.st.brands]}

    async def opener_momentum(self, opener_ids: list[str] | None = None) -> dict:
        ids = opener_ids or [o["id"] for o in self.st.openers[:3]]
        if not ids:
            raise ToolError("No openers yet; run find_openers first.")
        end = dt.date.today()
        start = end - dt.timedelta(days=84)
        res = {}
        for oid in ids[:3]:
            try:
                body = await self.call("momentum", "/v2/trending", {
                    "filter.type": "urn:entity:artist", "signal.interests.entities": oid,
                    "filter.start_date": start.isoformat(), "filter.end_date": end.isoformat(), "take": 12})
            except QlooCallFailed as f:
                res[oid] = {"error": f.err.category}
                continue
            rows = ((body.get("results") or {}).get("trending")) or []
            rows = sorted([r for r in rows if r.get("date")], key=lambda r: r["date"])
            if not rows:
                res[oid] = {"label": "no data"}
                continue
            first, last = _num(rows[0].get("population_percentile"), 0), _num(rows[-1].get("population_percentile"), 0)
            delta = round(last - first, 2)
            label = "rising" if delta > 2 else ("cooling" if delta < -2 else "steady")
            res[oid] = {"label": label, "percentile_now": last, "delta_12w": delta,
                        "series": [_num(r.get("population_percentile"), 0) for r in rows]}
        self.st.momentum.update(res)
        names = {o["id"]: o["name"] for o in self.st.openers}
        return {"momentum": {names.get(k, k): {kk: vv for kk, vv in v.items() if kk != "series"}
                             for k, v in res.items()}}

    async def explain_overlap(self, opener_id: str | None = None) -> dict:
        h = self.need_headliner()
        opener = next((o for o in self.st.openers if o["id"] == opener_id), None) if opener_id else None
        opener = opener or self.st.selected_opener
        if not opener:
            raise ToolError("No opener selected.")
        self.st.selected_opener = opener
        compare_tags: list[dict] = []
        try:
            body = await self.call("overlap", "/v2/analysis/compare", {
                "a.signal.interests.entities": h["id"], "b.signal.interests.entities": opener["id"], "take": 10})
            compare_tags = _parse_compare(body)
        except QlooCallFailed:
            self.st.notes.append("analysis/compare unavailable; overlap explained from taste analysis only.")
        tag_sets = []
        for eid in (h["id"], opener["id"]):
            body = await self.call("overlap", "/v2/insights",
                                   {"filter.type": "urn:tag", "signal.interests.entities": eid, "take": 20})
            tags = {}
            for t in ((body.get("results") or {}).get("tags")) or []:
                tid = t.get("tag_id") or t.get("id")
                if tid:
                    tags[tid] = {"name": t.get("name"), "affinity": _num((t.get("query") or {}).get("affinity"), 0.0)}
            tag_sets.append(tags)
        shared = [{"name": tag_sets[0][k]["name"], "headliner": tag_sets[0][k]["affinity"],
                   "opener": tag_sets[1][k]["affinity"]} for k in tag_sets[0] if k in tag_sets[1]]
        shared.sort(key=lambda s: -(s["headliner"] or 0) * (s["opener"] or 0))
        self.st.overlap = {"opener": opener["name"], "opener_id": opener["id"],
                           "compare": compare_tags[:8], "shared_tags": shared[:8]}
        return {"opener": opener["name"], "shared_taste_tags": [s["name"] for s in shared[:6]],
                "compare_top": [c["name"] for c in compare_tags[:5]]}


def _parse_compare(body: dict) -> list[dict]:
    res = body.get("results")
    items = []
    if isinstance(res, dict):
        items = res.get("tags") or res.get("entities") or []
    elif isinstance(res, list):
        items = res
    out = []
    for t in items:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        q = t.get("query") or {}
        a = _num((q.get("a") or {}).get("affinity")) if isinstance(q.get("a"), dict) else None
        b = _num((q.get("b") or {}).get("affinity")) if isinstance(q.get("b"), dict) else None
        out.append({"name": t["name"], "a": a, "b": b, "score": _num(q.get("affinity"), _num(t.get("popularity")))})
    return out
