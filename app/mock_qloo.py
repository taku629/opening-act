"""Synthetic Qloo responses for demo mode.

Nothing here is copied from a real Qloo response. Every value is generated
deterministically from the request parameters (seeded RNG) so that demos are
repeatable, and the *shapes* follow the documented response formats:

* /search                     -> {"success", "results": [entity, ...]}
* /v2/tags                    -> {"success", "results": {"tags": [...]}}
* /v2/insights entity types   -> {"success", "results": {"entities": [...]}}
* /v2/insights urn:demographics -> {"results": {"demographics": [{"entity_id", "query": {"age", "gender"}}]}}
* /v2/insights urn:heatmap    -> {"results": {"heatmap": [{"location", "query"}]}, "query": {"localities": ...}}
* /v2/insights urn:tag        -> {"results": {"tags": [{"tag_id", "name", "subtype", "query"}]}}
* /v2/trending                -> {"results": {"trending": [{"date", "population_percentile", ...}]}}
* /v2/analysis/compare        -> {"results": {"tags": [...]}} (shape not fully documented; see README)

Names of openers, venues and brands are invented on purpose so demo output can
never be mistaken for real Qloo data.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import random
import uuid
from typing import Any

from .geo import CITIES, city_key, geohash_encode

_NS = uuid.UUID("6f1c2a52-8d1e-4c3b-9a77-0b6b1d2e3f40")

_ADJ = ["Velvet", "Paper", "Neon", "Quiet", "Golden", "Hollow", "Lunar", "Static", "Copper", "Glass",
        "Summer", "Violet", "Cinder", "Marble", "Satellite", "Tender", "Wild", "Silver", "Coral", "Electric"]
_NOUN = ["Harbor", "Lanterns", "Parade", "Weather", "Orchard", "Signals", "Ghosts", "Motel", "Daydream",
         "Rivers", "Mirrors", "Pines", "Telephones", "Postcards", "Tigers", "Bloom", "Comets", "Gardens"]
_FIRST = ["Mira", "Juno", "Ezra", "Noa", "Rhea", "Theo", "Ines", "Kai", "Lena", "Otis", "Yuki", "Sol"]
_LAST = ["Kade", "Vale", "Marlow", "Reyes", "Sato", "Lind", "Okafor", "Brandt", "Quinn", "Hart"]
_VENUE_A = ["The Lantern", "Echo Chamber", "Night Owl", "Sideroom", "Basement", "Half Moon", "Larkspur",
            "Copper Door", "Lowlands", "Riverside", "Low Light", "Parlour", "Annex", "Rooftop"]
_VENUE_B = ["Hall", "Room", "Social Club", "Lounge", "Tavern", "Ballroom", "Listening Bar", "Theatre"]
_BRAND = [("Northbound Coffee Co.", "coffee"), ("Fieldnote Denim", "apparel"), ("Lowtide Brewing", "beer"),
          ("Arcadia Turntables", "audio"), ("Paperplane Film Lab", "photo"), ("Kinfolk Kombucha", "beverage"),
          ("Sundial Thrift", "retail"), ("Wavelength Headphones", "audio"), ("Greenroom Vinyl", "records"),
          ("Odd Hours Bikes", "mobility"), ("Hearth Pizza Club", "food"), ("Saltwater Skate", "apparel"),
          ("Moth & Moon Candles", "lifestyle"), ("Riverbend Cider", "beverage"), ("Atlas Camera Supply", "photo")]
_GENRES = [("indie_pop", "Indie Pop"), ("indie_rock", "Indie Rock"), ("dream_pop", "Dream Pop"),
           ("shoegaze", "Shoegaze"), ("bedroom_pop", "Bedroom Pop"), ("art_pop", "Art Pop"),
           ("synth_pop", "Synth Pop"), ("alternative", "Alternative"), ("folk", "Folk"),
           ("lo_fi", "Lo-Fi"), ("post_punk", "Post-Punk"), ("singer_songwriter", "Singer-Songwriter")]


def _rng(*parts: Any) -> random.Random:
    h = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()
    return random.Random(int(h[:16], 16))


def _eid(kind: str, name: str) -> str:
    return str(uuid.uuid5(_NS, f"{kind}:{name.lower()}")).upper()


def _artist_name(r: random.Random) -> str:
    if r.random() < 0.55:
        return f"{r.choice(_ADJ)} {r.choice(_NOUN)}"
    return f"{r.choice(_FIRST)} {r.choice(_LAST)}"


def _genre_tags(r: random.Random, k: int = 3) -> list[dict[str, str]]:
    return [{"id": f"urn:tag:genre:music:{g}", "name": n, "type": "urn:tag:genre:music"}
            for g, n in r.sample(_GENRES, k)]


def _artist_entity(name: str, popularity: float, r: random.Random, affinity: float | None = None) -> dict:
    ent = {
        "name": name,
        "entity_id": _eid("artist", name),
        "type": "urn:entity",
        "subtype": "urn:entity:artist",
        "types": ["urn:entity:artist"],
        "properties": {
            "short_description": "Synthetic demo artist",
            "image": None,
        },
        "popularity": round(popularity, 4),
        "tags": _genre_tags(r),
        "disambiguation": "demo data",
    }
    if affinity is not None:
        ent["query"] = {"affinity": round(affinity, 4),
                        "measurements": {"audience_growth": round(r.uniform(-0.05, 0.35), 4)}}
    return ent


def respond(path: str, params: dict[str, Any]) -> dict[str, Any]:
    if path == "/search":
        return _search(params)
    if path == "/v2/tags":
        return _tags(params)
    if path == "/v2/trending":
        return _trending(params)
    if path == "/v2/analysis/compare":
        return _compare(params)
    if path == "/v2/insights":
        t = params.get("filter.type")
        if t == "urn:entity:artist":
            return _artists(params)
        if t == "urn:entity:brand":
            return _brands(params)
        if t == "urn:entity:place":
            return _places(params)
        if t == "urn:demographics":
            return _demographics(params)
        if t == "urn:heatmap":
            return _heatmap(params)
        if t == "urn:tag":
            return _tag_insights(params)
    return {"success": False, "results": {}, "message": f"mock: unsupported {path}"}


def _search(p):
    q = str(p.get("query", "")).strip()
    if not q:
        return {"success": True, "results": [], "duration": 3}
    r = _rng("search", q.lower())
    head = _artist_entity(q.title() if q.islower() else q, r.uniform(0.93, 0.995), r)
    head["properties"]["short_description"] = "Demo headliner (synthetic profile)"
    others = [_artist_entity(f"{q} Tribute Band" if i == 0 else f"{q} & Friends", r.uniform(0.2, 0.5), r)
              for i in range(2)]
    take = int(p.get("take", 5) or 5)
    return {"success": True, "results": [head, *others][:take], "duration": 21}


def _tags(p):
    q = str(p.get("filter.query", "")).lower()
    catalog = [
        ("urn:tag:category:place:music_venue", "Music Venue", "urn:tag:category:place"),
        ("urn:tag:category:place:live_music_venue", "Live Music Venue", "urn:tag:category:place"),
        ("urn:tag:category:place:concert_hall", "Concert Hall", "urn:tag:category:place"),
        ("urn:tag:category:place:bar", "Bar", "urn:tag:category:place"),
        ("urn:tag:category:place:cocktail_bar", "Cocktail Bar", "urn:tag:category:place"),
        ("urn:tag:category:place:night_club", "Night Club", "urn:tag:category:place"),
        ("urn:tag:category:place:record_store", "Record Store", "urn:tag:category:place"),
    ]
    words = [w for w in q.replace("-", " ").split() if w]
    hits = [c for c in catalog if all(w in c[1].lower() for w in words)] or catalog[:2]
    return {"success": True, "duration": 9, "results": {"tags": [
        {"id": i, "tag_id": i, "name": n, "type": t, "types": ["urn:entity:place"]} for i, n, t in hits]}}


def _signal_ids(p) -> list[str]:
    return [s for s in str(p.get("signal.interests.entities", "")).split(",") if s]


def _artists(p):
    sig = ",".join(_signal_ids(p))
    loc = p.get("signal.location.query", "")
    r = _rng("artists", sig, loc, p.get("filter.popularity.max"), p.get("filter.popularity.min"))
    take = int(p.get("take", 20) or 20)
    pmax = float(p.get("filter.popularity.max", 1) or 1)
    pmin = float(p.get("filter.popularity.min", 0) or 0)
    seen, ents = set(), []
    aff = r.uniform(0.96, 0.995)
    while len(ents) < take:
        name = _artist_name(r)
        if name in seen:
            continue
        seen.add(name)
        pop = r.uniform(max(pmin, 0.25), min(pmax, 0.985))
        ents.append(_artist_entity(name, pop, r, affinity=aff))
        aff = max(0.3, aff - r.uniform(0.004, 0.03))
    body = {"success": True, "results": {"entities": ents}, "duration": 61}
    if loc:
        body["query"] = {"locality": {"signal": {"name": str(loc).title(), "subtype": "urn:entity:locality",
                                                 "entity_id": _eid("locality", str(loc))}}}
    return body


def _brands(p):
    r = _rng("brands", ",".join(_signal_ids(p)))
    take = int(p.get("take", 20) or 20)
    pool = _BRAND[:]
    r.shuffle(pool)
    aff, ents = r.uniform(0.9, 0.98), []
    for name, cat in pool[:take]:
        ents.append({
            "name": name, "entity_id": _eid("brand", name), "type": "urn:entity", "subtype": "urn:entity:brand",
            "properties": {"short_description": f"Synthetic demo {cat} brand"},
            "popularity": round(r.uniform(0.35, 0.9), 4),
            "tags": [{"id": f"urn:tag:category:brand:{cat}", "name": cat.title(), "type": "urn:tag:category:brand"}],
            "query": {"affinity": round(aff, 4)},
        })
        aff = max(0.3, aff - r.uniform(0.01, 0.05))
    return {"success": True, "results": {"entities": ents}, "duration": 48}


def _places(p):
    city = p.get("filter.location.query") or p.get("signal.location.query") or "Demo City"
    info = CITIES.get(city_key(city) or "")
    r = _rng("places", ",".join(_signal_ids(p)), city, p.get("filter.tags"), p.get("filter.popularity.max"))
    take = int(p.get("take", 20) or 20)
    pmax = float(p.get("filter.popularity.max", 1) or 1)
    ents, used = [], set()
    aff = r.uniform(0.9, 0.99)
    tag_ids = [t for t in str(p.get("filter.tags", "")).split(",") if t] or ["urn:tag:category:place:music_venue"]
    for _ in range(take):
        name = f"{r.choice(_VENUE_A)} {r.choice(_VENUE_B)}"
        if name in used:
            continue
        used.add(name)
        if info:
            hood, lat, lng = r.choice(info[3])
            lat, lng = lat + r.uniform(-0.006, 0.006), lng + r.uniform(-0.006, 0.006)
            addr = f"{r.randint(100, 3999)} {r.choice(['Main', 'Division', 'Mercer', 'Grand', 'Lake', 'Oak'])} St (demo), {hood}, {info[0]}"
        else:
            hood, lat, lng, addr = "Downtown", 0.0, 0.0, f"Demo address, {city}"
        tag = r.choice(tag_ids)
        ents.append({
            "name": name, "entity_id": _eid("place", name + str(city)), "type": "urn:entity",
            "subtype": "urn:entity:place",
            "location": {"lat": round(lat, 5), "lon": round(lng, 5), "geohash": geohash_encode(lat, lng, 9)},
            "properties": {"address": addr, "business_rating": round(r.uniform(3.9, 4.9), 1),
                           "neighborhood": hood, "is_closed": False},
            "popularity": round(r.uniform(0.3, min(pmax, 0.97)), 4),
            "tags": [{"id": tag, "name": tag.rsplit(":", 1)[-1].replace("_", " ").title(),
                      "type": "urn:tag:category:place"}],
            "query": {"affinity": round(aff, 4)},
        })
        aff = max(0.3, aff - r.uniform(0.01, 0.05))
    return {"success": True, "results": {"entities": ents}, "duration": 73}


def _demographics(p):
    out = []
    for sid in _signal_ids(p):
        r = _rng("demo", sid)
        ages = ["24_and_younger", "25_to_29", "30_to_34", "35_to_44", "45_to_54", "55_and_older"]
        peak = r.choice([1, 1, 2, 2, 0])
        age = {a: round(max(-0.6, min(0.8, 0.55 - 0.32 * abs(i - peak) + r.uniform(-0.08, 0.08))), 2)
               for i, a in enumerate(ages)}
        g = round(r.uniform(-0.25, 0.25), 2)
        out.append({"entity_id": sid, "query": {"age": age, "gender": {"male": g, "female": -g}}})
    return {"success": True, "results": {"demographics": out}, "duration": 40}


def _heatmap(p):
    city = p.get("filter.location.query") or "Demo City"
    info = CITIES.get(city_key(city) or "")
    r = _rng("heat", ",".join(_signal_ids(p)), city)
    take = int(p.get("take", 40) or 40)
    if info:
        clat, clng, hoods = info[1], info[2], info[3]
    else:
        clat, clng, hoods = 0.0, 0.0, [("Area", 0.0, 0.0)]
    hot = r.sample(hoods, k=min(3, len(hoods)))
    pts = []
    for i in range(take):
        if i < take * 0.6:
            _, hlat, hlng = r.choice(hot)
            lat, lng = hlat + r.gauss(0, 0.008), hlng + r.gauss(0, 0.008)
        else:
            lat, lng = clat + r.uniform(-0.12, 0.12), clng + r.uniform(-0.15, 0.15)
        pts.append((lat, lng))
    rows = []
    n = len(pts)
    for i, (lat, lng) in enumerate(pts):
        rows.append({"location": {"latitude": round(lat, 6), "longitude": round(lng, 6),
                                  "geohash": geohash_encode(lat, lng, 6)},
                     "query": {"affinity": round(1 - i / (n * 1.4), 4),
                               "affinity_rank": round(1 - i / (n * 1.6), 4),
                               "popularity": round(r.uniform(0.2, 0.98), 4)}})
    disp = info[0] if info else str(city)
    return {"success": True, "results": {"heatmap": rows},
            "query": {"localities": {"filter": {"entity_id": _eid("locality", disp), "name": disp,
                                                "subtype": "urn:entity:locality",
                                                # documented example lists lat/lng swapped; mirror that quirk
                                                "location": {"lat": clng, "lng": clat},
                                                "popularity": 1, "disambiguation": f"{disp} (demo)"}}},
            "duration": 75}


def _tag_insights(p):
    sig = ",".join(_signal_ids(p))
    r = _rng("tags", sig)
    take = int(p.get("take", 20) or 20)
    extra = [("keyword:music:melancholic", "Melancholic", "urn:tag:keyword:music"),
             ("keyword:music:introspective", "Introspective", "urn:tag:keyword:music"),
             ("keyword:music:danceable", "Danceable", "urn:tag:keyword:music"),
             ("keyword:music:nostalgic", "Nostalgic", "urn:tag:keyword:music"),
             ("keyword:music:lush_production", "Lush Production", "urn:tag:keyword:music")]
    pool = [(f"genre:music:{g}", n, "urn:tag:genre:music") for g, n in _GENRES] + extra
    r.shuffle(pool)
    aff, out = r.uniform(0.9, 0.99), []
    for tid, name, sub in pool[:take]:
        full = f"urn:tag:{tid}"
        out.append({"tag_id": full, "name": name, "types": ["urn:entity:artist"], "subtype": sub,
                    "tag_value": full, "query": {"affinity": round(aff, 4)}})
        aff = max(0.2, aff - r.uniform(0.01, 0.06))
    return {"success": True, "results": {"tags": out}, "duration": 25}


def _trending(p):
    sid = ",".join(_signal_ids(p))
    r = _rng("trend", sid)
    end = dt.date.fromisoformat(str(p.get("filter.end_date") or dt.date.today().isoformat()))
    start = dt.date.fromisoformat(str(p.get("filter.start_date") or (end - dt.timedelta(days=84)).isoformat()))
    weeks = max(1, min(int(p.get("take", 12) or 12), (end - start).days // 7 + 1))
    slope = r.uniform(-0.6, 1.8)
    pct = r.uniform(40, 80)
    rows = []
    for i in range(weeks):
        d = start + dt.timedelta(days=7 * i)
        prev = pct
        pct = max(1.0, min(99.9, pct + slope + r.uniform(-1.2, 1.2)))
        rows.append({"date": d.isoformat(), "population_percentile": round(pct, 2),
                     "population_rank": int(5000 * (1 - pct / 100)) + 1,
                     "population_rank_velocity": int((pct - prev) * 30),
                     "velocity_fold_change": round(1 + (pct - prev) / 50, 3),
                     "population_percent_delta": round((pct - prev) / max(prev, 1), 4)})
    rows.reverse()  # documented: ordered by date descending
    return {"success": True, "results": {"trending": rows}, "duration": 33}


def _compare(p):
    a = p.get("a.signal.interests.entities", "")
    b = p.get("b.signal.interests.entities", "")
    r = _rng("cmp", a, b)
    pool = _GENRES[:]
    r.shuffle(pool)
    out = []
    for g, n in pool[: int(p.get("take", 8) or 8)]:
        sa, sb = r.uniform(0.3, 0.99), r.uniform(0.3, 0.99)
        out.append({"tag_id": f"urn:tag:genre:music:{g}", "name": n, "subtype": "urn:tag:genre:music",
                    "query": {"a": {"affinity": round(sa, 4)}, "b": {"affinity": round(sb, 4)},
                              "affinity": round(min(sa, sb), 4)}})
    out.sort(key=lambda t: -t["query"]["affinity"])
    return {"success": True, "results": {"tags": out}, "duration": 52}
