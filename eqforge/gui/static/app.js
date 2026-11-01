/* EQForge GUI — vanilla JS, no dependencies. Talks JSON to the daemon. */
"use strict";

// ---------------------------------------------------------------- api --
const api = {
  async get(method) {
    const r = await fetch("/api/" + method);
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || "request failed");
    return j.result;
  },
  async post(method, params) {
    const r = await fetch("/api/" + method, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params || {}),
    });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || "request failed");
    return j.result;
  },
};

// ------------------------------------------------------------- state --
const S = {
  profiles: [],
  currentId: null,
  resolved: null,      // resolved profile dict (editable)
  dirty: false,
  selectedBand: -1,
  status: null,
  spectrum: null,      // {freqs, db} overlay from analysis
  rt: null,
  hoverBand: -1,
};

const FILTER_TYPES = ["peak", "lowshelf", "highshelf", "lowpass", "highpass",
                      "bandpass", "notch", "allpass"];
const TYPE_SHORT = { peak: "PK", lowshelf: "LS", highshelf: "HS",
                     lowpass: "LP", highpass: "HP", bandpass: "BP",
                     notch: "NT", allpass: "AP" };

// ------------------------------------------------------------ biquad --
function designBiquad(type, f0, gainDb, q, sr, useQ) {
  const A = Math.pow(10, gainDb / 40);
  let f = Math.min(Math.max(f0, 1), sr / 2 - 1);
  const w0 = 2 * Math.PI * f / sr;
  const cw = Math.cos(w0), sw = Math.sin(w0);
  let alpha;
  const isShelf = ["lowshelf", "highshelf"].includes(type);
  if (useQ === false && q > 0 && q <= 1) {
    alpha = sw / 2 * Math.sqrt((A + 1 / A) * (1 / q - 1) + 2);
  } else {
    alpha = sw / (2 * Math.max(q, 1e-4));
  }
  let b0, b1, b2, a0, a1, a2;
  const sqA = Math.sqrt(A), t2 = 2 * sqA * alpha;
  switch (type) {
    case "peak":
      b0 = 1 + alpha * A; b1 = -2 * cw; b2 = 1 - alpha * A;
      a0 = 1 + alpha / A; a1 = -2 * cw; a2 = 1 - alpha / A; break;
    case "lowpass":
      b0 = (1 - cw) / 2; b1 = 1 - cw; b2 = (1 - cw) / 2;
      a0 = 1 + alpha; a1 = -2 * cw; a2 = 1 - alpha; break;
    case "highpass":
      b0 = (1 + cw) / 2; b1 = -(1 + cw); b2 = (1 + cw) / 2;
      a0 = 1 + alpha; a1 = -2 * cw; a2 = 1 - alpha; break;
    case "bandpass":
      b0 = alpha; b1 = 0; b2 = -alpha;
      a0 = 1 + alpha; a1 = -2 * cw; a2 = 1 - alpha; break;
    case "notch":
      b0 = 1; b1 = -2 * cw; b2 = 1;
      a0 = 1 + alpha; a1 = -2 * cw; a2 = 1 - alpha; break;
    case "allpass":
      b0 = 1 - alpha; b1 = -2 * cw; b2 = 1 + alpha;
      a0 = 1 + alpha; a1 = -2 * cw; a2 = 1 - alpha; break;
    case "lowshelf":
      b0 = A * ((A + 1) - (A - 1) * cw + t2); b1 = 2 * A * ((A - 1) - (A + 1) * cw);
      b2 = A * ((A + 1) - (A - 1) * cw - t2);
      a0 = (A + 1) + (A - 1) * cw + t2; a1 = -2 * ((A - 1) + (A + 1) * cw);
      a2 = (A + 1) + (A - 1) * cw - t2; break;
    case "highshelf":
      b0 = A * ((A + 1) + (A - 1) * cw + t2); b1 = -2 * A * ((A - 1) + (A + 1) * cw);
      b2 = A * ((A + 1) + (A - 1) * cw - t2);
      a0 = (A + 1) - (A - 1) * cw + t2; a1 = 2 * ((A - 1) - (A + 1) * cw);
      a2 = (A + 1) - (A - 1) * cw - t2; break;
    default:
      return { b: [1, 0, 0], a: [1, 0, 0] };
  }
  return { b: [b0 / a0, b1 / a0, b2 / a0], a: [1, a1 / a0, a2 / a0] };
}

function magnitudeDb(biquad, f, sr) {
  const w = 2 * Math.PI * f / sr;
  const cw1 = Math.cos(w), sw1 = Math.sin(w);
  const cw2 = Math.cos(2 * w), sw2 = Math.sin(2 * w);
  const { b, a } = biquad;
  const br = b[0] + b[1] * cw1 + b[2] * cw2;
  const bi = -(b[1] * sw1 + b[2] * sw2);
  const ar = a[0] + a[1] * cw1 + a[2] * cw2;
  const ai = -(a[1] * sw1 + a[2] * sw2);
  const num = br * br + bi * bi;
  const den = ar * ar + ai * ai;
  if (den < 1e-30) return -120;
  return 10 * Math.log10(num / den);
}

// ------------------------------------------------------------ helpers --
const $ = (sel) => document.querySelector(sel);
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const db2lin = (db) => Math.pow(10, db / 20);

function toast(msg, isError) {
  const el = document.createElement("div");
  el.className = "toast" + (isError ? " error" : "");
  el.textContent = msg;
  $("#toast-area").appendChild(el);
  setTimeout(() => el.remove(), isError ? 6000 : 3200);
}

function autoPreamp(filters) {
  let boost = 0;
  for (const f of filters) {
    if (f.enabled !== false && (f.gain_db || 0) > 0) boost += f.gain_db;
  }
  return -Math.max(0, boost);
}

function currentFilters() {
  return ((S.resolved || {}).eq || {}).filters || [];
}

function currentDsp() {
  const filters = currentFilters();
  const pre = (S.resolved || {}).preamp || {};
  const preamp = pre.mode === "manual" ? (pre.gain_db || 0) : autoPreamp(filters);
  return { preamp, filters };
}

function markDirty(v = true) {
  S.dirty = v;
  $("#dirty-flag").classList.toggle("visible", v);
}

// --------------------------------------------------------- eq canvas --
const FMIN = 20, FMAX = 20000, SR_VIEW = 48000;
let GAIN_RANGE = 18;

function freqToX(f, w, padL, padR) {
  return padL + (w - padL - padR) * (Math.log10(clamp(f, FMIN, FMAX)) - Math.log10(FMIN)) /
    (Math.log10(FMAX) - Math.log10(FMIN));
}
function xToFreq(x, w, padL, padR) {
  const t = (x - padL) / (w - padL - padR);
  return Math.pow(10, Math.log10(FMIN) + t * (Math.log10(FMAX) - Math.log10(FMIN)));
}
function dbToY(db, h, padT, padB, ymax) {
  return padT + (h - padT - padB) * (ymax - clamp(db, -ymax, ymax)) / (2 * ymax);
}
function yToDb(y, h, padT, padB, ymax) {
  const t = (y - padT) / (h - padT - padB);
  return clamp(ymax - t * 2 * ymax, -ymax, ymax);
}

function computeCurve(filters, preamp, freqs) {
  const designs = filters.filter(f => f.enabled !== false)
    .map(f => designBiquad(f.type || "peak", f.freq, f.gain_db || 0,
                           f.q || 1.0, SR_VIEW, f.use_q));
  const out = new Float64Array(freqs.length);
  for (let i = 0; i < freqs.length; i++) {
    let sum = preamp;
    for (const d of designs) sum += magnitudeDb(d, freqs[i], SR_VIEW);
    out[i] = sum;
  }
  return out;
}

function drawEQ() {
  const canvas = $("#eq-canvas");
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  if (rect.width < 10) return;
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;
  const padL = 46, padR = 12, padT = 14, padB = 26;

  // auto gain range
  const { preamp, filters } = currentDsp();
  const freqs = new Float64Array(400);
  for (let i = 0; i < 400; i++)
    freqs[i] = Math.pow(10, Math.log10(FMIN) + (Math.log10(FMAX) - Math.log10(FMIN)) * i / 399);
  const curve = computeCurve(filters, preamp, freqs);
  let peak = 0;
  for (const v of curve) peak = Math.max(peak, Math.abs(v));
  GAIN_RANGE = clamp(Math.ceil((peak + 2) / 6) * 6, 12, 30);
  const ymax = GAIN_RANGE;

  ctx.clearRect(0, 0, W, H);

  // grid
  ctx.font = "10px Inter, sans-serif";
  ctx.textAlign = "center";
  for (const dec of [1, 2]) {
    for (const m of [1, 2, 5]) {
      const f = m * Math.pow(10, dec);
      if (f > FMAX) continue;
      const x = freqToX(f, W, padL, padR);
      ctx.strokeStyle = "#22252f"; ctx.beginPath();
      ctx.moveTo(x, padT); ctx.lineTo(x, H - padB); ctx.stroke();
      ctx.fillStyle = "#6b7183";
      ctx.fillText(f >= 1000 ? (f / 1000) + "k" : String(f), x, H - 9);
    }
  }
  const step = ymax * 2 > 36 ? 12 : 6;
  ctx.textAlign = "right";
  for (let g = -Math.floor(ymax / step) * step; g <= ymax; g += step) {
    const y = dbToY(g, H, padT, padB, ymax);
    ctx.strokeStyle = g === 0 ? "#3a3f4d" : "#1e212b";
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - padR, y); ctx.stroke();
    ctx.fillStyle = "#6b7183";
    ctx.fillText((g > 0 ? "+" : "") + g, padL - 6, y + 3);
  }

  // spectrum overlay
  if (S.spectrum && S.spectrum.freqs && S.spectrum.freqs.length > 4) {
    const sf = S.spectrum.freqs, sd = S.spectrum.db;
    let lo = 250, hi = 8000, acc = [], cnt = 0;
    for (let i = 0; i < sf.length; i++)
      if (sf[i] >= lo && sf[i] <= hi) { acc.push(sd[i]); cnt++; }
    const ref = acc.length ? acc.sort((a, b) => a - b)[Math.floor(acc.length / 2)] : 0;
    ctx.strokeStyle = "rgba(138,144,162,.55)"; ctx.lineWidth = 1.5;
    ctx.beginPath();
    let started = false;
    for (let i = 0; i < sf.length; i++) {
      if (sf[i] < FMIN || sf[i] > FMAX) continue;
      const v = sd[i] - ref;
      if (Math.abs(v) > ymax) { started = false; continue; }
      const x = freqToX(sf[i], W, padL, padR), y = dbToY(v, H, padT, padB, ymax);
      if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.lineWidth = 1;
  }

  // per-band ghosts
  for (let i = 0; i < filters.length; i++) {
    const f = filters[i];
    if (f.enabled === false) continue;
    const d = designBiquad(f.type || "peak", f.freq, f.gain_db || 0,
                           f.q || 1, SR_VIEW, f.use_q);
    ctx.strokeStyle = i === S.selectedBand ? "rgba(122,162,247,.85)"
                                             : "rgba(122,162,247,.35)";
    ctx.lineWidth = i === S.selectedBand ? 1.8 : 1.2;
    ctx.beginPath();
    for (let k = 0; k < freqs.length; k++) {
      const v = magnitudeDb(d, freqs[k], SR_VIEW);
      const x = freqToX(freqs[k], W, padL, padR);
      const y = dbToY(v, H, padT, padB, ymax);
      k === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.stroke();
  }

  // total curve (filled)
  ctx.beginPath();
  for (let i = 0; i < freqs.length; i++) {
    const x = freqToX(freqs[i], W, padL, padR);
    const y = dbToY(curve[i], H, padT, padB, ymax);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  }
  ctx.strokeStyle = "#9ece6a"; ctx.lineWidth = 2.5; ctx.stroke();
  ctx.lineTo(freqToX(FMAX, W, padL, padR), dbToY(0, H, padT, padB, ymax));
  ctx.lineTo(freqToX(FMIN, W, padL, padR), dbToY(0, H, padT, padB, ymax));
  ctx.closePath();
  ctx.fillStyle = "rgba(158,206,106,.07)"; ctx.fill();
  ctx.lineWidth = 1;

  // band handles
  for (let i = 0; i < filters.length; i++) {
    const f = filters[i];
    if (f.enabled === false) continue;
    const x = freqToX(f.freq, W, padL, padR);
    const y = dbToY(f.gain_db || 0, H, padT, padB, ymax);
    const sel = i === S.selectedBand, hov = i === S.hoverBand;
    ctx.beginPath();
    ctx.arc(x, y, sel || hov ? 8 : 6, 0, Math.PI * 2);
    ctx.fillStyle = sel ? "#7aa2f7" : (hov ? "#5d7fc4" : "#31415f");
    ctx.fill();
    ctx.strokeStyle = "#cdd6f4"; ctx.stroke();
    ctx.fillStyle = "#e8eaf2";
    ctx.textAlign = "center"; ctx.font = "bold 8.5px Inter, sans-serif";
    ctx.fillText(TYPE_SHORT[f.type] || "PK", x, y + 3);
  }
  ctx.font = "10px Inter, sans-serif";
}

// -------------------------------------------------------- eq interaction --
let drag = null;

function bandAt(mx, my) {
  const canvas = $("#eq-canvas");
  const rect = canvas.getBoundingClientRect();
  const W = rect.width, H = rect.height;
  const padL = 46, padR = 12, padT = 14, padB = 26;
  const filters = currentFilters();
  let best = -1, bestD = 1e9;
  for (let i = 0; i < filters.length; i++) {
    const f = filters[i];
    if (f.enabled === false) continue;
    const x = freqToX(f.freq, W, padL, padR);
    const y = dbToY(f.gain_db || 0, H, padT, padB, GAIN_RANGE);
    const d = Math.hypot(x - mx, y - my);
    if (d < 14 && d < bestD) { bestD = d; best = i; }
  }
  return best;
}

function initEQInteraction() {
  const canvas = $("#eq-canvas");
  const geom = () => {
    const r = canvas.getBoundingClientRect();
    return { W: r.width, H: r.height, padL: 46, padR: 12, padT: 14, padB: 26 };
  };
  canvas.addEventListener("pointerdown", (e) => {
    const r = canvas.getBoundingClientRect();
    const mx = e.clientX - r.left, my = e.clientY - r.top;
    const idx = bandAt(mx, my);
    if (idx >= 0) {
      S.selectedBand = idx;
      drag = { idx };
      canvas.setPointerCapture(e.pointerId);
      renderBands(); drawEQ();
    }
  });
  canvas.addEventListener("pointermove", (e) => {
    const r = canvas.getBoundingClientRect();
    const mx = e.clientX - r.left, my = e.clientY - r.top;
    if (drag) {
      const { W, H, padL, padR, padT, padB } = geom();
      const f = currentFilters()[drag.idx];
      f.freq = Math.round(clamp(xToFreq(mx, W, padL, padR), 10, 24000) * 10) / 10;
      if (!["lowpass", "highpass", "notch", "bandpass", "allpass"].includes(f.type))
        f.gain_db = Math.round(yToDb(my, H, padT, padB, GAIN_RANGE) * 10) / 10;
      markDirty(); renderBands(); drawEQ(); updatePreamp();
    } else {
      const idx = bandAt(mx, my);
      if (idx !== S.hoverBand) { S.hoverBand = idx; drawEQ(); }
      canvas.style.cursor = idx >= 0 ? "grab" : "crosshair";
    }
  });
  canvas.addEventListener("pointerup", () => { drag = null; });
  canvas.addEventListener("wheel", (e) => {
    const r = canvas.getBoundingClientRect();
    const idx = bandAt(e.clientX - r.left, e.clientY - r.top);
    if (idx >= 0) {
      e.preventDefault();
      const f = currentFilters()[idx];
      f.q = clamp(Math.round((f.q || 1) * (e.deltaY < 0 ? 1.15 : 1 / 1.15) * 100) / 100,
                  0.1, 40);
      S.selectedBand = idx;
      markDirty(); renderBands(); drawEQ();
    }
  }, { passive: false });
  canvas.addEventListener("dblclick", (e) => {
    const r = canvas.getBoundingClientRect();
    const mx = e.clientX - r.left, my = e.clientY - r.top;
    if (bandAt(mx, my) >= 0) return;
    const { W, H, padL, padR, padT, padB } = geom();
    const filters = currentFilters();
    filters.push({
      type: "peak",
      freq: Math.round(xToFreq(mx, W, padL, padR)),
      gain_db: Math.round(yToDb(my, H, padT, padB, GAIN_RANGE) * 10) / 10,
      q: 1.0, enabled: true,
    });
    S.selectedBand = filters.length - 1;
    markDirty(); renderBands(); drawEQ(); updatePreamp();
  });
  window.addEventListener("keydown", (e) => {
    if (e.key === "Delete" && S.selectedBand >= 0 &&
        document.activeElement.tagName !== "INPUT") {
      currentFilters().splice(S.selectedBand, 1);
      S.selectedBand = -1;
      markDirty(); renderBands(); drawEQ(); updatePreamp();
    }
    if (e.key === " " && document.activeElement.tagName !== "INPUT") {
      e.preventDefault(); toggleBypass();
    }
  });
}

// ----------------------------------------------------------- band table --
function renderBands() {
  const tbody = $("#band-table tbody");
  tbody.innerHTML = "";
  const filters = currentFilters();
  filters.forEach((f, i) => {
    const tr = document.createElement("tr");
    tr.className = i === S.selectedBand ? "selected" : "";
    const isGainless = ["lowpass", "highpass", "notch", "bandpass", "allpass"].includes(f.type);
    tr.innerHTML = `
      <td class="muted">${i + 1}</td>
      <td>${sel("type", FILTER_TYPES, f.type || "peak", i)}</td>
      <td><input type="number" data-i="${i}" data-k="freq" min="10" max="24000"
           step="any" value="${f.freq}"></td>
      <td>${isGainless ? '<span class="muted">—</span>' :
        `<input type="number" data-i="${i}" data-k="gain_db" min="-30" max="30"
           step="0.1" value="${f.gain_db || 0}">`}</td>
      <td><input type="number" data-i="${i}" data-k="q" min="0.1" max="40"
           step="0.01" value="${f.q || 1}"></td>
      <td><input type="checkbox" data-i="${i}" data-k="enabled"
           ${f.enabled !== false ? "checked" : ""}></td>
      <td><button class="btn small ghost" data-del="${i}" title="remove">✕</button></td>`;
    tr.addEventListener("click", (e) => {
      if (e.target.tagName !== "INPUT" && e.target.tagName !== "SELECT" &&
          e.target.tagName !== "BUTTON") {
        S.selectedBand = i; renderBands(); drawEQ();
      }
    });
    tbody.appendChild(tr);
  });
  tbody.querySelectorAll("input,select").forEach((el) => {
    el.addEventListener("change", () => {
      const i = +el.dataset.i, k = el.dataset.k;
      const f = currentFilters()[i];
      if (el.type === "checkbox") f[k] = el.checked;
      else if (el.tagName === "SELECT") f[k] = el.value;
      else f[k] = parseFloat(el.value);
      if (k === "type") renderBands();
      markDirty(); drawEQ(); updatePreamp();
    });
  });
  tbody.querySelectorAll("button[data-del]").forEach((b) =>
    b.addEventListener("click", () => {
      currentFilters().splice(+b.dataset.del, 1);
      S.selectedBand = -1; markDirty(); renderBands(); drawEQ(); updatePreamp();
    }));
}

function sel(name, options, value, i) {
  return `<select data-i="${i}" data-k="${name}">` +
    options.map(o => `<option ${o === value ? "selected" : ""}>${o}</option>`).join("") +
    `</select>`;
}

function updatePreamp() {
  const { preamp } = currentDsp();
  $("#m-preamp").textContent = preamp.toFixed(1) + " dB";
}

// --------------------------------------------------------- chain strip --
function renderChain() {
  const r = S.resolved || {};
  const stages = [
    { key: "crossfeed", label: "Crossfeed",
      params: [["level_db", "dB"], ["fc_hz", "Hz"]] },
    { key: "compressor", label: "Compressor",
      params: [["threshold_db", "thr"], ["ratio", ":1"]] },
    { key: "limiter", label: "Limiter",
      params: [["ceiling_db", "ceil dB"], ["lookahead_ms", "LA ms"]] },
  ];
  const strip = $("#chain-strip");
  strip.innerHTML = "";
  // preamp chip
  const pre = r.preamp || {};
  const chip = document.createElement("div");
  chip.className = "stage on";
  chip.innerHTML = `<span class="stage-dot"></span>Preamp
    <span class="stage-params">
      <select id="preamp-mode">
        <option value="auto" ${pre.mode !== "manual" ? "selected" : ""}>auto</option>
        <option value="manual" ${pre.mode === "manual" ? "selected" : ""}>manual</option>
      </select>
      <input type="number" id="preamp-gain" step="0.5" style="width:64px"
        value="${(pre.gain_db || 0).toFixed(1)}" ${pre.mode !== "manual" ? "disabled" : ""}>
    </span>`;
  strip.appendChild(chip);
  chip.querySelector("#preamp-mode").addEventListener("change", (e) => {
    S.resolved.preamp = { ...(S.resolved.preamp || {}), mode: e.target.value };
    if (e.target.value === "manual")
      S.resolved.preamp.gain_db = parseFloat(chip.querySelector("#preamp-gain").value) || 0;
    markDirty(); updatePreamp(); renderChain();
  });
  chip.querySelector("#preamp-gain").addEventListener("change", (e) => {
    S.resolved.preamp = { ...(S.resolved.preamp || {}), mode: "manual",
                          gain_db: parseFloat(e.target.value) || 0 };
    markDirty(); updatePreamp();
  });

  for (const st of stages) {
    const cfg = r[st.key] || {};
    const el = document.createElement("div");
    el.className = "stage" + (cfg.enabled ? " on" : "");
    let paramsHtml = "";
    if (cfg.enabled) {
      paramsHtml = `<span class="stage-params">` + st.params.map(([k, unit]) =>
        `<input type="number" step="any" data-stage="${st.key}" data-p="${k}"
          value="${cfg[k] ?? ""}" title="${unit}">`).join("") + `</span>`;
    }
    el.innerHTML = `<span class="stage-dot"></span>${st.label}${paramsHtml}`;
    el.addEventListener("click", (e) => {
      if (e.target.tagName === "INPUT") return;
      S.resolved[st.key] = { ...(S.resolved[st.key] || defaults(st.key)),
                             enabled: !(cfg.enabled) };
      markDirty(); renderChain();
    });
    strip.appendChild(el);
  }
  strip.querySelectorAll("input[data-stage]").forEach((inp) => {
    inp.addEventListener("change", () => {
      const st = inp.dataset.stage, p = inp.dataset.p;
      S.resolved[st] = { ...(S.resolved[st] || {}), [p]: parseFloat(inp.value) };
      markDirty();
    });
    inp.addEventListener("click", (e) => e.stopPropagation());
  });
}

function defaults(stage) {
  if (stage === "limiter") return { enabled: true, ceiling_db: -1, attack_ms: 5,
    release_ms: 60, lookahead_ms: 1.5, knee_db: 3 };
  if (stage === "crossfeed") return { enabled: false, level_db: -6.5, fc_hz: 700 };
  if (stage === "compressor") return { enabled: false, threshold_db: -18,
    ratio: 3, knee_db: 6, attack_ms: 10, release_ms: 120, makeup_db: 0 };
  return {};
}

// ---------------------------------------------------------- profiles --
async function loadProfiles() {
  const r = await api.get("list_profiles");
  S.profiles = r.profiles;
  const select = $("#profile-select");
  const list = $("#profile-list");
  select.innerHTML = "";
  list.innerHTML = "";
  const q = ($("#profile-search").value || "").toLowerCase();
  for (const p of S.profiles) {
    const opt = document.createElement("option");
    opt.value = p.id; opt.textContent = p.name;
    if (p.id === S.currentId) opt.selected = true;
    select.appendChild(opt);
    if (q && !(p.name + p.id + p.tags.join()).toLowerCase().includes(q)) continue;
    const item = document.createElement("div");
    item.className = "profile-item" + (p.id === S.currentId ? " selected" : "");
    item.innerHTML = `<div class="pname">${esc(p.name)}</div>
      <div class="pmeta">${p.builtin ? "builtin" : "user"}${p.tags.length ? " · " + esc(p.tags.slice(0, 3).join(", ")) : ""}</div>`;
    item.addEventListener("click", () => activateProfile(p.id));
    list.appendChild(item);
  }
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;",
    ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function activateProfile(id, force) {
  if (S.dirty && !force &&
      !confirm("Discard unsaved changes to the current profile?")) return;
  try {
    const r = await api.post("resolve_profile", { profile: id });
    S.currentId = id;
    S.resolved = JSON.parse(JSON.stringify(r.resolved));
    S.selectedBand = -1;
    S.dirty = false;
    markDirty(false);
    $("#profile-title").textContent =
      (S.resolved.name || id) + (S.resolved.builtin ? "  (builtin)" : "");
    $("#profile-select").value = id;
    document.querySelectorAll(".profile-item").forEach((el, i) =>
      el.classList.toggle("selected", false));
    await loadProfiles();
    renderBands(); renderChain(); drawEQ(); updatePreamp();
    const st = await api.get("status");
    if (st.active_profile !== id) {
      await api.post("set_profile", { profile: id });
      toast("Activated “" + (S.resolved.name || id) + "”");
    }
  } catch (e) { toast(String(e.message), true); }
}

// ------------------------------------------------------------ save --
async function saveProfile(saveAs) {
  const r = S.resolved;
  let id = r.id, name = r.name;
  if (saveAs || r.builtin) {
    const res = await modal(
      saveAs ? "Save as new profile" : "Built-in profiles are read-only — save a copy",
      [{ label: "id", value: saveAs ? "" : id + "-custom" },
       { label: "name", value: name + (saveAs ? "" : " (custom)") }]);
    if (!res) return;
    id = (res.id || "").trim(); name = (res.name || "").trim();
    if (!/^[a-zA-Z0-9][a-zA-Z0-9._-]*$/.test(id)) {
      toast("Invalid id (letters, digits, . _ -)", true); return;
    }
  }
  const prof = { ...JSON.parse(JSON.stringify(r)), id, name, builtin: false };
  delete prof.exported_from; delete prof.exported_at;
  try {
    await api.post("save_profile", { profile: prof });
    await api.post("set_profile", { profile: id });
    toast("Saved " + id);
    await loadProfiles();
    await activateProfile(id, true);
  } catch (e) { toast(String(e.message), true); }
}

function modal(title, fields) {
  return new Promise((resolve) => {
    const back = $("#modal-backdrop");
    $("#modal-title").textContent = title;
    const body = $("#modal-body");
    body.innerHTML = "";
    const inputs = [];
    for (const f of fields) {
      const inp = document.createElement("input");
      inp.placeholder = f.label; inp.value = f.value || "";
      body.appendChild(inp); inputs.push([f.label, inp]);
    }
    back.classList.remove("hidden");
    inputs[0] && inputs[0][1].focus();
    const done = (val) => {
      back.classList.add("hidden");
      $("#modal-ok").onclick = null; $("#modal-cancel").onclick = null;
      resolve(val);
    };
    $("#modal-ok").onclick = () => done(Object.fromEntries(
      inputs.map(([k, i]) => [k, i.value])));
    $("#modal-cancel").onclick = () => done(null);
  });
}

// ---------------------------------------------------------- analysis --
async function analyzeFile() {
  const path = $("#file-path").value.trim();
  if (!path) { toast("Enter an audio file path", true); return; }
  const box = $("#analysis-result");
  box.innerHTML = '<div class="muted">analyzing…</div>';
  try {
    const r = await api.post("analyze_file", { path });
    renderAnalysis(r);
  } catch (e) {
    box.innerHTML = `<div class="finding warn"><div class="fmsg">${esc(e.message)}</div></div>`;
  }
}

function renderAnalysis(r) {
  const rep = r.report;
  const box = $("#analysis-result");
  S.spectrum = rep.spectrum && rep.spectrum.freqs ? rep.spectrum : null;
  drawEQ();
  const fmt = (v, unit = "", digits = 1) =>
    v === null || v === undefined ? "—" :
      (typeof v === "number" ? v.toFixed(digits) : v) + " " + unit;
  box.innerHTML = `
    <div class="ar-section"><h4>${esc(r.path.split("/").pop())}</h4>
      <div class="ar-grid">
        <span class="k">integrated</span><span class="v">${fmt(rep.integrated_lufs, "LUFS")}</span>
        <span class="k">loudness range</span><span class="v">${fmt(rep.loudness_range_lu, "LU")}</span>
        <span class="k">peak</span><span class="v">${fmt(rep.peak_db, "dBFS", 2)}</span>
        <span class="k">true peak</span><span class="v">${fmt(rep.true_peak_db, "dBTP", 2)}</span>
        <span class="k">crest factor</span><span class="v">${fmt(rep.crest_factor_db, "dB")}</span>
        <span class="k">duration</span><span class="v">${fmt(rep.duration_s, "s", 1)}</span>
      </div></div>
    <canvas class="spectrum-canvas" id="spec-canvas"></canvas>
    <div class="ar-section"><h4>Advisor</h4>${
      r.advice.findings.map(f => `
      <div class="finding ${f.severity}">
        <div class="fmsg">${esc(f.message)}</div>
        ${f.hint ? `<div class="fhint">${esc(f.hint)}</div>` : ""}
      </div>`).join("")}</div>`;
  drawMiniSpectrum();
}

function drawMiniSpectrum() {
  const c = $("#spec-canvas");
  if (!c || !S.spectrum) return;
  const dpr = window.devicePixelRatio || 1;
  const rect = c.getBoundingClientRect();
  c.width = rect.width * dpr; c.height = rect.height * dpr;
  const ctx = c.getContext("2d"); ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;
  const { freqs, db } = S.spectrum;
  const lo = Math.min(...db), hi = Math.max(...db);
  ctx.strokeStyle = "#7aa2f7"; ctx.beginPath();
  for (let i = 0; i < freqs.length; i++) {
    const x = (Math.log10(freqs[i]) - Math.log10(20)) /
              (Math.log10(20000) - Math.log10(20)) * W;
    const y = H - (db[i] - lo) / (hi - lo + 1e-9) * (H - 8) - 4;
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  }
  ctx.stroke();
}

// ------------------------------------------------------------ system --
async function loadDevices() {
  try {
    const r = await api.get("devices");
    const sel = $("#device-select");
    sel.innerHTML = "";
    if (!r.available) {
      sel.innerHTML = "<option>PipeWire not available</option>";
      return;
    }
    for (const d of r.devices.filter(x => x.direction === "sink")) {
      const opt = document.createElement("option");
      opt.value = d.node_name;
      opt.textContent = `${d.description} [${d.kind}]`;
      if (d.node_name === r.default_sink) opt.selected = true;
      sel.appendChild(opt);
    }
    const cur = r.devices.find(d => d.node_name === r.default_sink);
    $("#device-info").textContent = cur ?
      `kind: ${cur.kind} · ${cur.media_class}` : "";
    $("#dot-pw").classList.add("ok");
  } catch { $("#dot-pw").classList.remove("ok"); }
}

async function loadStreams() {
  try {
    const r = await api.get("streams");
    const el = $("#stream-list");
    if (!r.streams.length) { el.textContent = "no active streams"; return; }
    el.innerHTML = r.streams.slice(0, 12).map(s =>
      `<div class="row"><span>${esc(s.name.slice(0, 26))}</span>
       <span class="muted">${esc(s.binary || "")}</span></div>`).join("");
  } catch { /* ignore */ }
}

// ------------------------------------------------------------- meters --
function renderMeters(rt) {
  S.rt = rt;
  const dot = $("#dot-rt");
  if (rt && rt.running) {
    dot.classList.add("ok");
    $("#m-lufs").textContent = (rt.momentary_lufs ?? -70).toFixed(1) + " LUFS";
    $("#m-lim").textContent = (rt.limiter_gain_db ?? 0).toFixed(1) + " dB";
    const sr = rt.sample_rate || 48000;
    $("#m-latency").textContent = rt.latency_frames != null ?
      `${rt.latency_frames}f · ${(1000 * rt.latency_frames / sr).toFixed(2)} ms` : "—";
    $("#rt-info").textContent =
      `running · ${rt.sample_rate} Hz · buffer ${rt.buffer_size}`;
    drawOutputMeter(rt.out_peak_db ?? -70);
  } else {
    dot.classList.remove("ok");
    $("#rt-info").textContent = "not running — start it below or run `eqforge system start`";
    $("#m-lufs").textContent = "— LUFS";
    $("#m-lim").textContent = "—";
    drawOutputMeter(-70);
  }
}

function drawOutputMeter(peakDb) {
  const c = $("#meter-canvas");
  const dpr = window.devicePixelRatio || 1;
  const rect = c.getBoundingClientRect();
  c.width = rect.width * dpr; c.height = rect.height * dpr;
  const ctx = c.getContext("2d"); ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;
  ctx.fillStyle = "#1a1d26"; ctx.fillRect(0, 0, W, H);
  const norm = clamp((peakDb + 60) / 60, 0, 1);
  const grad = ctx.createLinearGradient(0, 0, W, 0);
  grad.addColorStop(0, "#9ece6a"); grad.addColorStop(0.75, "#e0af68");
  grad.addColorStop(0.95, "#f7768e");
  ctx.fillStyle = grad;
  ctx.fillRect(2, 3, (W - 4) * norm, H - 6);
  // ticks
  ctx.fillStyle = "rgba(255,255,255,.25)";
  for (const t of [-48, -24, -12, -6, -1]) {
    const x = 2 + (W - 4) * (t + 60) / 60;
    ctx.fillRect(x, 0, 1, 4);
  }
}

// ------------------------------------------------------------- events --
function connectSSE() {
  const es = new EventSource("/api/events");
  es.addEventListener("meters", (e) => {
    const d = JSON.parse(e.data);
    renderMeters(d.rt);
  });
  es.addEventListener("profile_changed", (e) => {
    const d = JSON.parse(e.data);
    toast(`Auto-switched to “${d.profile}”`);
    activateProfile(d.profile, true);
  });
  es.addEventListener("graph_changed", () => { loadDevices(); loadStreams(); });
  es.addEventListener("profiles_changed", () => loadProfiles());
  es.onerror = () => { $("#dot-daemon").classList.remove("ok"); };
  es.onopen = () => { $("#dot-daemon").classList.add("ok"); };
}

// ---------------------------------------------------------------- init --
async function refreshStatus() {
  try {
    const st = await api.get("status");
    S.status = st;
    $("#dot-daemon").classList.add("ok");
    $("#slot-a").classList.toggle("active", st.active_slot === "A");
    $("#slot-b").classList.toggle("active", st.active_slot === "B");
    $("#slot-a").title = "Slot A: " + st.slot_a;
    $("#slot-b").title = "Slot B: " + st.slot_b;
    $("#bypass-btn").classList.toggle("on", st.bypass);
    $("#auto-switch").checked = !!st.auto_switch;
    if (!S.currentId || (!S.dirty && st.active_profile !== S.currentId))
      await activateProfile(st.active_profile, true);
    renderMeters(st.rt);
  } catch {
    $("#dot-daemon").classList.remove("ok");
  }
}

async function toggleBypass() {
  try {
    const r = await api.post("bypass", {});
    $("#bypass-btn").classList.toggle("on", r.bypass);
    toast(r.bypass ? "Bypass ON — audio passthrough" : "Bypass off");
  } catch (e) { toast(String(e.message), true); }
}

function init() {
  $("#engine-badge").textContent = "loading…";
  initEQInteraction();
  $("#profile-search").addEventListener("input", loadProfiles);
  $("#profile-select").addEventListener("change", (e) =>
    activateProfile(e.target.value));
  $("#bypass-btn").addEventListener("click", toggleBypass);
  $("#slot-a").addEventListener("click", async () => {
    await api.post("ab", { slot: "A" }); refreshStatus(); });
  $("#slot-b").addEventListener("click", async () => {
    await api.post("ab", { slot: "B" }); refreshStatus(); });
  $("#save-btn").addEventListener("click", () => saveProfile(false));
  $("#saveas-btn").addEventListener("click", () => saveProfile(true));
  $("#revert-btn").addEventListener("click", () =>
    S.currentId && activateProfile(S.currentId, true));
  $("#new-profile").addEventListener("click", async () => {
    const res = await modal("New profile",
      [{ label: "id", value: "" }, { label: "name", value: "" }]);
    if (!res || !res.id.trim()) return;
    try {
      await api.post("save_profile", { profile: {
        format: "eqforge.profile", format_version: 2,
        id: res.id.trim(), name: res.name.trim() || res.id.trim(),
        preamp: { mode: "auto" }, eq: { filters: [] },
        limiter: defaults("limiter"),
      }});
      await loadProfiles();
      await activateProfile(res.id.trim());
      toast("Created " + res.id.trim());
    } catch (e) { toast(String(e.message), true); }
  });
  $("#import-hint").addEventListener("click", () =>
    toast("Import from the terminal: eqforge import <file> (AutoEQ, REW, APO, CSV)"));
  $("#analyze-btn").addEventListener("click", analyzeFile);
  $("#file-path").addEventListener("keydown", (e) => {
    if (e.key === "Enter") analyzeFile(); });
  document.querySelectorAll(".tab").forEach((t) =>
    t.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
      document.querySelectorAll(".tab-body").forEach(x => x.classList.remove("active"));
      t.classList.add("active");
      $("#tab-" + t.dataset.tab).classList.add("active");
      if (t.dataset.tab === "system") { loadDevices(); loadStreams(); }
    }));
  $("#auto-switch").addEventListener("change", async (e) => {
    await api.post("auto_switch", { value: e.target.checked });
    toast(e.target.checked ? "Auto-switch enabled" : "Auto-switch disabled");
  });
  $("#rt-start").addEventListener("click", async () => {
    toast("Starting the RT host needs a terminal: `eqforge system start`");
  });

  window.addEventListener("resize", () => { drawEQ(); drawMiniSpectrum(); });

  connectSSE();
  (async () => {
    await loadProfiles();
    await refreshStatus();
    await loadDevices();
    try {
      const nat = await api.post("rt_command", { cmd: { cmd: "status" } });
      $("#engine-badge").textContent =
        (nat.rt && nat.rt.running) ? `native · ${nat.rt.sample_rate} Hz` : "native core";
    } catch { $("#engine-badge").textContent = "native core"; }
  })();
  setInterval(refreshStatus, 5000);
  setInterval(() => {
    if ($("#tab-system").classList.contains("active")) loadStreams();
  }, 4000);
}

document.addEventListener("DOMContentLoaded", init);
