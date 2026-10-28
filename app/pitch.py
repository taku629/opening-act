"""Pitch writing (LLM with template fallback), the 'LLM alone' comparison, and
Markdown export of the full brief."""
from __future__ import annotations

import json
from typing import Any

from .tools import RunState

AGE_LABELS = {"24_and_younger": "24 and under", "25_to_29": "25–29", "30_to_34": "30–34", "35_to_44": "35–44",
              "45_to_54": "45–54", "55_and_older": "55+", "35_and_younger": "35 and under", "36_to_55": "36–55"}


def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.0f}%"


def facts(st: RunState) -> dict[str, Any]:
    o = st.selected_opener or {}
    m = st.momentum.get(o.get("id", ""), {}) if o else {}
    d = st.demographics or {}
    return {
        "headliner": (st.headliner or {}).get("name"), "city": st.inputs["city"],
        "capacity": st.inputs.get("capacity"), "show_date": st.inputs.get("show_date"),
        "opener": o.get("name"), "opener_affinity": o.get("affinity"), "opener_popularity": o.get("popularity"),
        "headliner_popularity": (st.headliner or {}).get("popularity"),
        "opener_momentum": {k: v for k, v in m.items() if k != "series"},
        "shared_taste_tags": [s["name"] for s in (st.overlap or {}).get("shared_tags", [])[:5]],
        "audience_top_age_band": AGE_LABELS.get(d.get("top_age"), d.get("top_age")),
        "audience_gender_skew": d.get("gender_skew"),
        "top_neighborhoods": [h["name"] for h in (st.heatmap or {}).get("neighborhoods", [])[:3]],
        "venues": [{"name": v["name"], "neighborhood": v.get("neighborhood"), "affinity": v["affinity"]}
                   for v in st.venues[:3]],
        "sponsor_brands": [{"name": b["name"], "affinity": b["affinity"]} for b in st.brands[:3]],
        "data_mode": "synthetic demo data" if st.mock else "live Qloo API",
    }


PITCH_SYSTEM = """You write concise booking pitches for independent music promoters.
Write in Markdown, max 230 words, addressed to the opener's booking agent.
Structure: a subject line in bold, 2 short paragraphs, then a 3-5 bullet 'Why the numbers work' list, then a one-line ask.
Cite ONLY the numbers in the JSON (affinity as %, neighborhoods, brands). Do not invent facts, fees, or capacities.
If data_mode is 'synthetic demo data', add a final italic line: '_Figures from demo data._'"""


async def write_pitch(client, model: str, st: RunState, recommendation: str = "") -> str | None:
    if client is None:
        return None
    payload = facts(st) | ({"agent_recommendation": recommendation} if recommendation else {})
    resp = await client.chat.completions.create(
        model=model, temperature=0.4, max_tokens=700,
        messages=[{"role": "system", "content": PITCH_SYSTEM},
                  {"role": "user", "content": json.dumps(payload, default=str)}])
    text = (resp.choices[0].message.content or "").strip()
    return text or None


async def llm_alone_answer(client, model: str, st: RunState) -> str:
    if client is None:
        return ""
    q = (f"Without any tools or data access: suggest 3 opening acts for a {st.inputs['headliner']} show in "
         f"{st.inputs['city']}, the neighborhoods to flyer, and 2 sponsor brands. Be brief (max 80 words). "
         f"Say how confident you are in audience overlap numbers.")
    resp = await client.chat.completions.create(model=model, temperature=0.7, max_tokens=200,
                                                messages=[{"role": "user", "content": q}])
    return (resp.choices[0].message.content or "").strip()


def template_pitch(st: RunState) -> str:
    f = facts(st)
    h, o, city = f["headliner"], f["opener"] or "your artist", f["city"]
    when = f" on {f['show_date']}" if f.get("show_date") else ""
    mom = f["opener_momentum"] or {}
    lines = [f"**Opening slot offer: {o} supporting {h} — {city}{when}**", ""]
    lines.append(f"Hi — I'm putting together {h}'s {city} date{when} and I'd love {o} on the bill. "
                 f"Qloo's taste graph puts {o} among the strongest audience overlaps with {h}, "
                 f"which means the room will already be primed for your set.")
    lines.append("")
    hoods = ", ".join(f["top_neighborhoods"]) or "the core of the city"
    lines.append(f"We'll target promo where {h}'s fans actually cluster — {hoods} — so your name reaches "
                 f"people who are likely to stay for the opener.")
    lines += ["", "**Why the numbers work**"]
    if f.get("opener_affinity") is not None:
        lines.append(f"- Audience affinity with {h}: **{pct(f['opener_affinity'])}** (Qloo insights)")
    if mom.get("label") and mom.get("label") != "no data":
        lines.append(f"- Momentum: **{mom['label']}** ({mom.get('delta_12w', 0):+} pts popularity percentile over 12 weeks)")
    if f["shared_taste_tags"]:
        lines.append(f"- Shared taste DNA: {', '.join(f['shared_taste_tags'][:4])}")
    if f.get("audience_top_age_band"):
        lines.append(f"- Core crowd skews **{f['audience_top_age_band']}**")
    if f["sponsor_brands"]:
        b = f["sponsor_brands"][0]
        lines.append(f"- Sponsor fit: {b['name']} ({pct(b['affinity'])} affinity) for a co-branded moment")
    if f["venues"]:
        v = f["venues"][0]
        lines.append(f"- Venue: {v['name']}{' (' + v['neighborhood'] + ')' if v.get('neighborhood') else ''}, "
                     f"{pct(v['affinity'])} taste match with {h}'s audience")
    poss = f"{o}'" if o.endswith("s") else f"{o}'s"
    lines += ["", f"Are {poss} dates open{when or ' for this run'}? Happy to send offer details."]
    if st.mock:
        lines += ["", "_Figures from demo data._"]
    return "\n".join(lines)


def brief_markdown(b: dict) -> str:
    inp = b["inputs"]
    h = b.get("headliner") or {}
    out = [f"# Booking brief: {h.get('name', inp['headliner'])} — {inp['city']}", ""]
    meta = [f"Capacity: {inp.get('capacity') or 'n/a'}", f"Date: {inp.get('show_date') or 'TBD'}",
            f"Data: {'DEMO (synthetic)' if b['mock'] else 'live Qloo API'}", f"Qloo calls: {b['qloo_calls']}",
            f"Planner: {b['planner']}"]
    out += [" · ".join(meta), ""]
    if b.get("pitch_md"):
        out += ["## Pitch", "", b["pitch_md"], ""]
    if b.get("openers"):
        out += ["## Opener candidates", "", "| Artist | Affinity | Popularity | Tier | Momentum |", "|---|---|---|---|---|"]
        for o in b["openers"][:8]:
            m = (b.get("momentum") or {}).get(o["id"], {})
            out.append(f"| {o['name']} | {pct(o['affinity'])} | {pct(o['popularity'])} | {o['tier']} | {m.get('label', '—')} |")
        out.append("")
    d = b.get("demographics")
    if d:
        out += ["## Audience profile", ""]
        out += [f"- {AGE_LABELS.get(k, k)}: {v:+.2f}" for k, v in d["age"].items()]
        out += [f"- {k}: {v:+.2f}" for k, v in d["gender"].items()] + [""]
    hm = b.get("heatmap")
    if hm and hm.get("neighborhoods"):
        out += ["## Where the audience lives", ""]
        out += [f"{i}. {n['name']} — {pct(n.get('share'))} of audience heat, peak affinity {n['affinity']:.2f}"
                for i, n in enumerate(hm["neighborhoods"], 1)]
        out.append("")
    if b.get("venues"):
        out += ["## Venues", ""] + [f"- **{v['name']}** — {v.get('neighborhood') or ''} · affinity {pct(v['affinity'])}"
                                    for v in b["venues"]] + [""]
    if b.get("brands"):
        out += ["## Sponsor / partner brands", ""] + [f"- **{x['name']}** — affinity {pct(x['affinity'])}"
                                                      for x in b["brands"]] + [""]
    ov = b.get("overlap")
    if ov and ov.get("shared_tags"):
        out += [f"## Why {ov['opener']} fits", ""] + [f"- {s['name']}" for s in ov["shared_tags"]] + [""]
    if b.get("notes"):
        out += ["## Notes", ""] + [f"- {n}" for n in b["notes"]] + [""]
    out += ["---", "Generated by Opening Act · taste data © Qloo. Affinities describe aggregate audiences, not individuals."]
    return "\n".join(out)
