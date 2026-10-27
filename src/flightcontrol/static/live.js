(function () {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const X = lon => 20 + (lon + 125) / 59 * 960;
  const Y = lat => 20 + (49.5 - lat) / 25 * 520;

  // A coarse outline of the lower 48, enough to give the airports a place.
  const US = [[-124.7,48.4],[-124.1,46.3],[-124.1,43.0],[-124.4,40.4],[-123.8,39.0],[-122.5,37.5],[-121.9,36.6],
    [-120.6,34.6],[-118.5,34.0],[-117.1,32.5],[-114.7,32.7],[-111.0,31.3],[-108.2,31.3],[-106.5,31.8],[-104.5,29.6],
    [-103.0,29.0],[-101.4,29.8],[-99.5,27.5],[-97.4,25.9],[-97.2,27.8],[-95.0,29.3],[-93.8,29.7],[-91.0,29.2],
    [-89.4,29.0],[-89.6,30.2],[-88.0,30.4],[-85.3,29.7],[-84.0,30.0],[-82.7,28.0],[-81.8,26.1],[-80.4,25.2],
    [-80.0,26.8],[-80.6,28.4],[-81.4,30.7],[-81.0,31.9],[-79.0,33.5],[-76.5,34.7],[-75.5,35.6],[-76.0,37.0],
    [-75.9,38.3],[-75.1,38.8],[-74.0,40.5],[-72.0,41.0],[-70.0,41.6],[-70.6,42.6],[-70.7,43.1],[-69.0,44.1],
    [-67.0,44.8],[-67.8,47.1],[-69.2,47.4],[-71.5,45.0],[-74.9,45.0],[-76.5,44.2],[-79.0,43.3],[-83.0,42.0],
    [-82.4,45.3],[-84.7,46.5],[-89.6,47.9],[-95.2,49.0],[-123.0,49.0],[-123.3,48.3]];
  document.getElementById("land").setAttribute("d",
    "M" + US.map(p => X(p[0]).toFixed(1) + "," + Y(p[1]).toFixed(1)).join("L") + "Z");

  const pos = {};
  const aps = document.getElementById("aps");
  for (const a of window.AIRPORTS) {
    pos[a.code] = [X(a.lon), Y(a.lat)];
    const g = document.createElementNS(NS, "g");
    g.setAttribute("class", "ap" + (a.hub ? " hub" : ""));
    const c = document.createElementNS(NS, "circle");
    c.setAttribute("cx", pos[a.code][0]); c.setAttribute("cy", pos[a.code][1]); c.setAttribute("r", a.hub ? 6 : 4.5);
    const t = document.createElementNS(NS, "text");
    t.setAttribute("x", pos[a.code][0] + 8); t.setAttribute("y", pos[a.code][1] - 7);
    t.textContent = a.code;
    const title = document.createElementNS(NS, "title");
    title.textContent = a.city;
    g.append(c, t, title);
    aps.appendChild(g);
  }

  const arcs = document.getElementById("arcs");
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const seen = new Set();

  function fly(o, d, house) {
    if (!pos[o] || !pos[d] || arcs.childNodes.length > 120) return;
    const [x1, y1] = pos[o], [x2, y2] = pos[d];
    const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
    const len = Math.hypot(x2 - x1, y2 - y1);
    const cx = mx - (y2 - y1) * 0.18, cy = my + (x2 - x1) * 0.18 - len * 0.12;
    const p = document.createElementNS(NS, "path");
    p.setAttribute("d", `M${x1},${y1} Q${cx},${cy} ${x2},${y2}`);
    p.setAttribute("class", "arc" + (house ? " house" : ""));
    arcs.appendChild(p);
    const L = p.getTotalLength();
    const dot = document.createElementNS(NS, "circle");
    dot.setAttribute("r", 3.2);
    dot.setAttribute("class", "plane" + (house ? " house" : ""));
    arcs.appendChild(dot);
    const dur = reduce ? 1 : 1600 + len * 2;
    const t0 = performance.now();
    function step(now) {
      const k = Math.min(1, (now - t0) / dur);
      p.style.strokeDasharray = L;
      p.style.strokeDashoffset = L * (1 - k);
      const pt = p.getPointAtLength(L * k);
      dot.setAttribute("cx", pt.x); dot.setAttribute("cy", pt.y);
      if (k < 1) requestAnimationFrame(step);
      else {
        dot.remove();
        p.style.transition = "opacity 2.5s";
        p.style.opacity = "0";
        setTimeout(() => p.remove(), 2600);
      }
    }
    requestAnimationFrame(step);
  }

  const money = c => "$" + Math.round(c / 100).toLocaleString();
  const clock = t => new Date(t * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", hour12: false});

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function renderTeams(teams) {
    const box = document.getElementById("teams");
    const on = teams.filter(t => t.mode !== "idle");
    document.getElementById("nteams").textContent = on.length;
    document.getElementById("tps").textContent = on.reduce((s, t) => s + (t.rate || 0), 0).toFixed(1);
    box.replaceChildren();
    if (!teams.length) { box.append(el("div", "empty", "Nobody is connected yet.")); return; }
    teams.sort((a, b) => (b.house - a.house) || a.name.localeCompare(b.name));
    for (const t of teams) {
      const card = el("div", "team");
      const r1 = el("div", "row1");
      r1.append(el("span", "name", t.name));
      const pill = el("span", "pill " + (t.mode === "steady" ? "steady" : t.mode.startsWith("test") ? "test" : ""), t.mode);
      r1.append(pill);
      card.append(r1);
      if (t.phase && t.mode.startsWith("test")) card.append(el("div", "small muted", t.phase));
      const nums = el("div", "nums");
      const cell = (label, v, cls) => { const d = el("div", cls); d.append(el("span", "", label)); d.append(document.createTextNode(v)); return d; };
      nums.append(cell("per sec", (t.rate || 0).toFixed(1)));
      nums.append(cell("ok 30s", t.ok ?? 0, "ok"));
      nums.append(cell("not ok", t.bad ?? 0, t.bad ? "no" : ""));
      nums.append(cell("p95 ms", t.p95_ms == null ? "-" : Math.round(t.p95_ms)));
      card.append(nums);
      box.append(card);
    }
  }

  function renderBoard(feed) {
    const box = document.getElementById("board");
    const rows = feed.slice(-14).reverse();
    if (!rows.length) return;
    box.replaceChildren();
    for (const e of rows) {
      const line = el("div", "line");
      line.append(el("span", "", clock(e.t)));
      if (e.kind === "book") {
        line.append(el("span", "route", `${e.origin}→${e.destination}`));
        line.append(el("span", "who", `${e.team} · ${e.pax} pax${e.stops ? " · 1 stop" : ""}`));
        line.append(el("span", "amt", money(e.cents || 0)));
      } else {
        line.append(el("span", "route cx", "CANCEL"));
        line.append(el("span", "who", e.team));
        line.append(el("span", "amt", ""));
      }
      box.append(line);
    }
  }

  async function poll() {
    try {
      const r = await fetch("/api/live", {cache: "no-store"});
      const s = await r.json();
      document.getElementById("bpm").textContent = s.bookings_per_min || 0;
      document.getElementById("stale").hidden = !s.stale;
      renderTeams(s.teams || []);
      renderBoard(s.feed || []);
      let n = 0;
      for (const e of (s.feed || [])) {
        if (e.kind !== "book") continue;
        const key = `${e.t}|${e.team}|${e.origin}|${e.destination}`;
        if (seen.has(key)) continue;
        seen.add(key);
        if (n++ < 12) setTimeout(() => fly(e.origin, e.destination, e.house), Math.random() * 1400);
      }
      if (seen.size > 5000) seen.clear();
    } catch (err) { /* try again next tick */ }
  }
  poll();
  setInterval(poll, 1500);
})();
