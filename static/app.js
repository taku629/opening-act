/* Opening Act front-end: streams the agent trace (SSE over fetch) and renders cards. */
(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const pct = (x) => (x == null || isNaN(x) ? "—" : `${Math.round(x * 100)}%`);
  const AGE = { "24_and_younger": "≤24", "25_to_29": "25–29", "30_to_34": "30–34", "35_to_44": "35–44", "45_to_54": "45–54", "55_and_older": "55+", "35_and_younger": "≤35", "36_to_55": "36–55" };
  const KEY_PARAMS = ["filter.type", "query", "types", "signal.interests.entities", "a.signal.interests.entities", "b.signal.interests.entities",
    "signal.location.query", "filter.location.query", "filter.tags", "filter.popularity.max", "filter.query", "filter.start_date", "take"];

  let state = { runId: null, brief: null, mock: true, busy: false, stepEls: {}, names: {} };

  // ---------- health / status ----------
  // Replay mode: ?replay=1, or when the page is opened without the backend (static hosting).
  // Plays back static/demo-trace.json, a recorded run on synthetic demo data.
  state.replay = new URLSearchParams(location.search).has("replay");
  fetch("api/health").then((r) => { if (!r.ok) throw new Error(); return r.json(); }).catch(() => {
    state.replay = true; return { mock: true, llm: false, replay: true };
  }).then((h) => {
    if (state.replay) h.replay = true;
    state.mock = h.mock;
    $("#demo-banner").hidden = !h.mock;
    $("#status-pills").innerHTML =
      `<span class="pill ${h.mock ? "demo" : "live"}">${h.mock ? "Qloo: demo data" : "Qloo: live API"}</span>` +
      `<span class="pill">${h.replay ? "Replay of a recorded run" : h.llm ? "Agent: " + esc(h.model) : "Agent: deterministic planner"}</span>`;
  });

  // ---------- form ----------
  document.querySelectorAll(".chip").forEach((b) => b.addEventListener("click", () => {
    $("#headliner").value = b.dataset.h; $("#city").value = b.dataset.c; $("#brief-form").requestSubmit();
  }));
  $("#brief-form").addEventListener("submit", (e) => {
    e.preventDefault();
    if (state.busy) return;
    const p = new URLSearchParams({ headliner: $("#headliner").value.trim(), city: $("#city").value.trim() });
    if ($("#capacity").value) p.set("capacity", $("#capacity").value);
    if ($("#show_date").value) p.set("show_date", $("#show_date").value);
    resetWorkspace();
    if (state.replay) replay(); else stream(`api/brief/stream?${p}`);
  });

  function resetWorkspace() {
    state.runId = null; state.brief = null; state.stepEls = {}; state.names = {}; state.selectedId = null; state.momentum = null; state.lastOpeners = null;
    $("#workspace").hidden = false;
    $("#steps").innerHTML = ""; $("#thoughts").innerHTML = ""; $("#trace-meta").textContent = "";
    ["#headliner-card", "#card-openers", "#card-audience", "#card-heat", "#card-venues", "#card-brands", "#card-overlap", "#card-pitch", "#card-compare", "#card-notes"]
      .forEach((s) => ($(s).hidden = true));
    $("#workspace").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ---------- streaming ----------
  async function stream(url) {
    setBusy(true);
    try {
      const res = await fetch(url, { headers: { Accept: "text/event-stream" } });
      if (!res.ok) {
        let msg = `Request failed (${res.status})`;
        try { msg = (await res.json()).error || msg; } catch (_) {}
        showError(msg); return;
      }
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
          const data = chunk.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trim()).join("\n");
          if (data) { try { handle(JSON.parse(data)); } catch (err) { console.error(err); } }
        }
      }
    } catch (err) {
      showError("Connection lost: " + err.message);
    } finally { setBusy(false); }
  }

  async function replay() {
    setBusy(true);
    try {
      const evs = await (await fetch("static/demo-trace.json")).json();
      const first = evs.find((e) => e.type === "run_start");
      if (first) { $("#headliner").value = first.inputs.headliner; $("#city").value = first.inputs.city; $("#capacity").value = first.inputs.capacity || ""; }
      for (const ev of evs) {
        handle(ev);
        await new Promise((r) => setTimeout(r, ev.type === "qloo_call" ? 160 : 60));
      }
      $("#dl-md").removeAttribute("href");
    } catch (e) { showError("Replay unavailable: " + e.message); }
    finally { setBusy(false); }
  }

  function setBusy(b) {
    state.busy = b;
    const btn = $("#go"); btn.disabled = b; btn.textContent = b ? "Agent working…" : "Build booking brief";
    document.querySelectorAll("[data-swap]").forEach((x) => (x.disabled = b));
  }

  function handle(ev) {
    switch (ev.type) {
      case "run_start": onRunStart(ev); break;
      case "step_start": stepStatus(ev.step, "running", ev.title); break;
      case "qloo_call": addCall(ev); break;
      case "step_done": stepStatus(ev.step, "done"); renderStep(ev.step, ev.data); break;
      case "step_error": stepStatus(ev.step, "error"); addThought(`${ev.title}: ${ev.error}`, true); break;
      case "agent_thought": addThought(ev.text); break;
      case "pitch": stepStatus("pitch", "done"); renderPitch(ev); break;
      case "done": onDone(ev.brief); break;
      case "error": showError(ev.error); break;
    }
  }

  function onRunStart(ev) {
    state.runId = ev.run_id;
    $("#trace-meta").textContent = ev.llm ? `LLM planner · ${ev.model}` : "deterministic planner";
    for (const s of ev.steps) ensureStep(s.step, s.title, s.endpoint);
    ensureStep("pitch", "Write booking pitch", ev.llm ? "LLM, grounded in the numbers above" : "template, grounded in the numbers above");
  }

  function ensureStep(step, title, ep) {
    if (state.stepEls[step]) return state.stepEls[step];
    const li = $("#tpl-step").content.firstElementChild.cloneNode(true);
    $(".step-title", li).textContent = title || step;
    $(".step-ep", li).textContent = ep || "";
    $("#steps").appendChild(li);
    state.stepEls[step] = li;
    return li;
  }
  function stepStatus(step, cls, title) {
    const li = ensureStep(step, title);
    li.classList.remove("running", "done", "error"); li.classList.add(cls);
    if (cls === "running") li.scrollIntoView({ block: "nearest" });
  }

  function addCall(c) {
    const li = ensureStep(c.step);
    const params = Object.entries(c.params || {}).filter(([k]) => KEY_PARAMS.includes(k))
      .map(([k, v]) => `${esc(k)}=${esc(shortVal(k, v))}`).join(" &amp; ");
    const div = document.createElement("div");
    div.className = "call";
    div.innerHTML = `<span class="m">GET</span> ${esc(c.endpoint)}<div class="p">${params}</div><div class="meta">` +
      (c.status === "ok" ? `<span class="tag ok">${c.count} results</span>` : `<span class="tag err">${esc(c.category || "error")} ${c.http_status || ""}</span>`) +
      `<span class="tag">${c.duration_ms} ms</span>` + (c.cached ? `<span class="tag cache">cached</span>` : "") +
      (c.mock ? `<span class="tag mock">demo</span>` : "") +
      ((c.stripped_params || []).length ? `<span class="tag err" title="Dropped: not valid for this filter.type">stripped ${c.stripped_params.length}</span>` : "") + `</div>`;
    if (c.status !== "ok" && c.error) div.title = c.error;
    $(".calls", li).appendChild(div);
  }
  function shortVal(k, v) {
    v = String(v);
    if (k.includes("entities")) return v.split(",").map((id) => state.names[id] || id.slice(0, 8) + "…").join(",");
    return v.length > 48 ? v.slice(0, 46) + "…" : v;
  }
  function addThought(t, warn) {
    const d = document.createElement("div"); d.className = "thought"; if (warn) d.style.borderColor = "var(--warn)";
    d.textContent = t; $("#thoughts").appendChild(d);
  }

  // ---------- renderers ----------
  function renderStep(step, data) {
    if (!data) return;
    ({ resolve: renderHeadliner, openers: renderOpeners, demographics: renderAudience, heatmap: renderHeat,
      venues: renderVenues, brands: renderBrands, momentum: (d) => { state.momentum = Object.assign({}, state.momentum || {}, d); renderOpeners(state.lastOpeners || []); }, overlap: renderOverlap }[step] || (() => {}))(data);
  }

  function renderHeadliner(h) {
    state.headliner = h; state.names[h.id] = h.name;
    const el = $("#headliner-card"); el.hidden = false;
    el.innerHTML = `<div><div class="sub">Headliner</div><div class="big">${esc(h.name)}</div>
      <div class="sub">${esc((h.tags || []).join(" · "))}</div></div>
      <div class="kpis"><div class="kpi"><div class="v">${pct(h.popularity)}</div><div class="l">Popularity pct.</div></div>
      <div class="kpi"><div class="v">${esc($("#city").value)}</div><div class="l">Market</div></div>
      <div class="kpi"><div class="v">${esc($("#capacity").value || "—")}</div><div class="l">Capacity</div></div></div>`;
  }

  function spark(series) {
    if (!series || series.length < 2) return "";
    const min = Math.min(...series), max = Math.max(...series), w = 64, h = 20;
    const pts = series.map((v, i) => `${(i / (series.length - 1)) * w},${h - 2 - ((v - min) / (max - min || 1)) * (h - 4)}`).join(" ");
    return `<svg class="spark" viewBox="0 0 ${w} ${h}"><polyline fill="none" stroke="currentColor" stroke-width="1.6" points="${pts}"/></svg>`;
  }

  function renderOpeners(rows) {
    state.lastOpeners = rows; rows.forEach((o) => (state.names[o.id] = o.name));
    $("#card-openers").hidden = false;
    const mom = state.momentum || (state.brief && state.brief.momentum) || {};
    const sel = state.selectedId || (state.brief && state.brief.selected_opener && state.brief.selected_opener.id) || (rows[0] && rows[0].id);
    // Show the momentum column only when Qloo /v2/trending actually returned a series for some opener.
    const hasMom = rows.slice(0, 8).some((o) => mom[o.id] && mom[o.id].label && mom[o.id].label !== "no data");
    $("#openers").innerHTML = `<table class="op-table"><thead><tr><th>Artist</th><th>Audience affinity</th><th class="hide-sm">Popularity</th><th>Tier</th>${hasMom ? `<th class="hide-sm">12-wk momentum</th>` : ""}<th></th></tr></thead><tbody>` +
      rows.slice(0, 8).map((o) => {
        const m = (mom[o.id] && mom[o.id].label !== "no data") ? mom[o.id] : {};
        const momHtml = m.label ? `<span class="mom ${esc(m.label)}">${spark(m.series)}${esc(m.label)}${m.delta_12w != null ? ` ${m.delta_12w > 0 ? "+" : ""}${m.delta_12w}` : ""}</span>` : `<span class="muted small">—</span>`;
        return `<tr class="${o.id === sel ? "selected" : ""}"><td><div class="op-name">${esc(o.name)}</div><div class="op-tags">${esc((o.tags || []).join(" · "))}</div></td>
        <td><div class="bar accent"><i style="width:${(o.affinity || 0) * 100}%"></i></div><span class="num">${pct(o.affinity)}</span></td>
        <td class="hide-sm"><div class="bar"><i style="width:${(o.popularity || 0) * 100}%"></i></div><span class="num">${pct(o.popularity)}</span></td>
        <td><span class="tier ${esc(o.tier)}">${esc(o.tier)}</span></td>${hasMom ? `<td class="hide-sm">${momHtml}</td>` : ""}
        <td>${o.id === sel ? `<span class="small muted">pitched</span>` : `<button class="btn tiny" data-swap="${esc(o.id)}" ${state.busy ? "disabled" : ""}>Pitch this act</button>`}</td></tr>`;
      }).join("") + `</tbody></table>`;
    document.querySelectorAll("[data-swap]").forEach((b) => b.addEventListener("click", () => swap(b.dataset.swap)));
  }

  function renderAudience(d) {
    $("#card-audience").hidden = false;
    const ages = Object.entries(d.age || {});
    const max = Math.max(0.01, ...ages.map(([, v]) => Math.abs(v)));
    const g = d.gender_skew;
    $("#audience").innerHTML = `<div class="small muted" style="margin-bottom:4px">Affinity vs. average (− under-index · + over-index)</div>` +
      ages.map(([k, v]) => `<div class="age-row"><span>${esc(AGE[k] || k)}</span><div class="div-bar"><i class="${v >= 0 ? "pos" : "neg"}" style="width:${(Math.abs(v) / max) * 50}%"></i></div><span class="num">${v > 0 ? "+" : ""}${v.toFixed(2)}</span></div>`).join("") +
      `<div class="callout">Core crowd: <strong>${esc(AGE[d.top_age] || d.top_age || "—")}</strong>${g ? ` · leans <strong>${esc(g.leans)}</strong> (${g.score > 0 ? "+" : ""}${g.score.toFixed(2)})` : ""}. Use it to set ticket price, set times and ad targeting.</div>`;
  }

  function renderHeat(h) {
    $("#card-heat").hidden = false;
    const all = h.points || [];
    // Live Qloo returns the whole city grid; plot the hottest cells so the fan core fills the frame.
    const pts = all.length > 300 ? all.slice().sort((p, q) => q.affinity - p.affinity).slice(0, 300) : all;
    let svg = "";
    if (pts.length) {
      const lats = pts.map((p) => p.lat), lngs = pts.map((p) => p.lng);
      const [a, b, c, d] = [Math.min(...lats), Math.max(...lats), Math.min(...lngs), Math.max(...lngs)];
      const W = 400, H = 170, pad = 12;
      const x = (lng) => pad + ((lng - c) / (d - c || 1)) * (W - 2 * pad);
      const y = (lat) => H - pad - ((lat - a) / (b - a || 1)) * (H - 2 * pad);
      const top = new Set((h.neighborhoods || []).slice(0, 3).map((n) => n.name));
      const labels = {};
      pts.forEach((p) => { if (top.has(p.label) && (!labels[p.label] || p.affinity > labels[p.label].affinity)) labels[p.label] = p; });
      svg = `<svg class="heatmap-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">` +
        pts.slice().sort((p, q) => p.affinity - q.affinity).map((p) => `<circle cx="${x(p.lng).toFixed(1)}" cy="${y(p.lat).toFixed(1)}" r="${3 + p.affinity * 6}" fill="rgba(255,77,46,${0.12 + p.affinity * 0.6})"/>`).join("") +
        (() => { // nudge labels apart vertically so they never overlap
          const ls = Object.values(labels).map((p) => ({ t: p.label, x: Math.min(W - 80, x(p.lng) + 8), y: y(p.lat) })).sort((m, n) => m.y - n.y);
          ls.forEach((l, i) => { l.y = Math.max(14, l.y); if (i && l.y - ls[i - 1].y < 14) l.y = ls[i - 1].y + 14; });
          return ls.map((l) => `<text x="${l.x.toFixed(1)}" y="${Math.min(H - 4, l.y).toFixed(1)}" font-size="12" font-family="Inter" font-weight="700" fill="#16130f" paint-order="stroke" stroke="#fff" stroke-width="4" stroke-linejoin="round">${esc(l.t)}</text>`).join("");
        })() + `</svg>`;
    }
    const maxShare = Math.max(0.01, ...(h.neighborhoods || []).map((n) => n.share || 0));
    $("#heat").innerHTML = `<div class="heat-wrap">${svg}<div>` + (h.neighborhoods || []).map((n, i) =>
      `<div class="hood"><span class="rank">${i + 1}</span><span>${esc(n.name)}</span><div class="bar accent"><i style="width:${((n.share || 0) / maxShare) * 100}%"></i></div><span class="num">${pct(n.share)}</span></div>`).join("") +
      `</div><div class="callout">Share of the headliner's audience heat in ${esc(h.city)}. Put street teams, posters and geo-targeted ads here first.${h.named ? "" : " (Areas labelled by coordinates — no neighborhood gazetteer for this city.)"}</div></div>`;
  }

  function renderVenues(rows) {
    $("#card-venues").hidden = false;
    $("#venues").innerHTML = rows.length ? `<ul class="list">` + rows.slice(0, 6).map((v) =>
      `<li><div><div class="t">${esc(v.name)}</div><div class="s">${esc([v.neighborhood, (v.tags || [])[0], v.rating ? "★ " + Number(v.rating).toFixed(1) : null].filter(Boolean).join(" · "))}</div></div>
      <div class="bar blue"><i style="width:${(v.affinity || 0) * 100}%"></i></div><span class="num">${pct(v.affinity)}</span></li>`).join("") + `</ul>
      <div class="callout">Taste match between each venue's patrons and the headliner's audience. Qloo has no capacity field; popularity is used as a soft size filter.</div>`
      : `<p class="muted">No venues matched.</p>`;
  }

  function renderBrands(rows) {
    $("#card-brands").hidden = false;
    $("#brands").innerHTML = rows.length ? `<ul class="list">` + rows.slice(0, 6).map((b) =>
      `<li><div><div class="t">${esc(b.name)}</div><div class="s">${esc((b.categories || []).join(" · "))}</div></div>
      <div class="bar accent"><i style="width:${(b.affinity || 0) * 100}%"></i></div><span class="num">${pct(b.affinity)}</span></li>`).join("") + `</ul>
      <div class="callout">Brands this audience over-indexes on — warm leads for a sponsored drink, merch collab or presale.</div>`
      : `<p class="muted">No brands returned.</p>`;
  }

  function renderOverlap(o) {
    $("#card-overlap").hidden = false;
    $("#overlap-title").textContent = `Why ${o.opener} fits`;
    state.selectedId = o.opener_id;
    const shared = o.shared_tags || [], cmp = o.compare || [];
    const head = state.headliner ? state.headliner.name : "Headliner";
    $("#overlap").innerHTML = `<div class="legend"><span><i style="background:var(--ink)"></i>${esc(head)}</span><span><i style="background:var(--accent)"></i>${esc(o.opener)}</span></div>
      <div class="overlap-grid"><div><div class="small muted">Shared taste tags (taste analysis)</div>` +
      (shared.length ? shared.slice(0, 6).map((s) => `<div class="ov-row"><span>${esc(s.name)}</span><div class="dual"><div class="bar"><i style="width:${(s.headliner || 0) * 100}%"></i></div><div class="bar accent"><i style="width:${(s.opener || 0) * 100}%"></i></div></div></div>`).join("") : `<p class="muted small">No shared tags returned.</p>`) +
      `</div><div><div class="small muted">Side-by-side (analysis/compare)</div>` +
      (cmp.length ? cmp.slice(0, 6).map((s) => `<div class="ov-row"><span>${esc(s.name)}</span><div class="dual"><div class="bar"><i style="width:${(s.a || s.score || 0) * 100}%"></i></div><div class="bar accent"><i style="width:${(s.b || s.score || 0) * 100}%"></i></div></div></div>`).join("") : `<p class="muted small">Compare endpoint returned nothing.</p>`) +
      `</div></div>`;
    if (state.lastOpeners) renderOpeners(state.lastOpeners);
  }

  function md(src) {
    const lines = esc(src || "").split("\n"); let html = "", inList = false;
    const inline = (s) => s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/(^|[^*])\*(?!\s)(.+?)\*/g, "$1<em>$2</em>").replace(/_(.+?)_/g, "<em>$1</em>");
    for (const l of lines) {
      if (/^\s*[-*] /.test(l)) { if (!inList) { html += "<ul>"; inList = true; } html += `<li>${inline(l.replace(/^\s*[-*] /, ""))}</li>`; continue; }
      if (inList) { html += "</ul>"; inList = false; }
      if (/^#{1,3} /.test(l)) html += `<h3>${inline(l.replace(/^#+ /, ""))}</h3>`;
      else if (l.trim()) html += `<p>${inline(l)}</p>`;
    }
    return html + (inList ? "</ul>" : "");
  }

  function renderPitch(ev) {
    $("#card-pitch").hidden = false;
    $("#pitch").innerHTML = md(ev.markdown);
    $("#pitch-src").textContent = ev.source === "template" ? "template · numbers from Qloo" : `written by ${ev.source.replace("llm:", "")} · numbers from Qloo`;
    state.pitch = ev.markdown; state.llmAlone = ev.llm_alone;
  }

  function onDone(b) {
    if (!b) return;
    state.brief = b; state.runId = b.run_id; state.momentum = b.momentum; state.selectedId = b.selected_opener ? b.selected_opener.id : state.selectedId;
    if (b.openers && b.openers.length) renderOpeners(b.openers);
    $("#dl-md").href = `api/runs/${b.run_id}/markdown`;
    $("#trace-meta").textContent = `${b.qloo_calls} Qloo calls · ${b.planner}`;
    renderCompare(b);
    const notes = [...(b.notes || [])];
    if (b.mock) notes.unshift("Demo mode: synthetic data shaped like Qloo responses. Set QLOO_API_KEY to go live.");
    notes.push("Affinities describe aggregate audiences, not individuals. They are evidence for a booking decision, not a guarantee of ticket sales.");
    $("#card-notes").hidden = false;
    $("#notes").innerHTML = notes.map((n) => `<li>${esc(n)}</li>`).join("");
  }

  function renderCompare(b) {
    $("#card-compare").hidden = false;
    const alone = b.llm_alone || "**Plausible names, no evidence.** A language model on its own can list artists in a similar genre, but it can't measure whether their fans overlap, can't tell who is rising this month, doesn't know which neighborhoods the crowd lives in, and can't rank brands by audience affinity. It would also confidently suggest acts far too big to open.\n\n_(Configure LLM_API_KEY to see a live model answer here.)_";
    $("#llm-alone").innerHTML = md(alone);
    const o = b.selected_opener || {}, m0 = (b.momentum || {})[o.id] || {}, m = m0.label === "no data" ? {} : m0, hm = b.heatmap || {}, d = b.demographics || {};
    const items = [
      o.name && `<strong>${esc(o.name)}</strong>: ${pct(o.affinity)} audience affinity, ${esc(o.tier)} (popularity ${pct(o.popularity)} vs ${pct((b.headliner || {}).popularity)})`,
      m.label && `Momentum: <strong>${esc(m.label)}</strong> (${m.delta_12w > 0 ? "+" : ""}${m.delta_12w} pts in 12 weeks)`,
      (hm.neighborhoods || []).length && `Flyer first: ${hm.neighborhoods.slice(0, 3).map((n) => `${esc(n.name)} (${pct(n.share)})`).join(", ")}`,
      d.top_age && `Core crowd ${esc(AGE[d.top_age] || d.top_age)}${d.gender_skew ? `, leans ${esc(d.gender_skew.leans)}` : ""}`,
      (b.brands || []).length && `Sponsor leads: ${b.brands.slice(0, 2).map((x) => `${esc(x.name)} (${pct(x.affinity)})`).join(", ")}`,
      `${b.qloo_calls} Qloo calls, every one visible in the trace`,
    ].filter(Boolean);
    $("#with-qloo").innerHTML = items.map((i) => `<li>${i}</li>`).join("");
  }

  async function swap(openerId) {
    if (!state.runId || state.busy) return;
    state.selectedId = openerId;
    $("#card-pitch").scrollIntoView({ behavior: "smooth", block: "center" });
    $("#pitch").innerHTML = `<div class="skeleton"></div><div class="skeleton" style="width:80%"></div><div class="skeleton" style="width:60%"></div>`;
    if (state.replay) { toast("Swapping openers needs the live backend"); return; }
    await stream(`api/runs/${state.runId}/swap/stream?opener_id=${encodeURIComponent(openerId)}`);
  }

  function showError(msg) {
    const box = document.createElement("div"); box.className = "error-box"; box.textContent = msg;
    $("#thoughts").appendChild(box);
    toast(msg);
  }
  function toast(msg) {
    const t = document.createElement("div"); t.className = "toast"; t.textContent = msg; document.body.appendChild(t);
    setTimeout(() => t.remove(), 3200);
  }

  $("#copy-pitch").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(state.pitch || ""); toast("Pitch copied"); } catch (_) { toast("Copy failed"); }
  });
  $("#print").addEventListener("click", () => window.print());
})();
