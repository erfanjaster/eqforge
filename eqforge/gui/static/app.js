/* EQForge GUI — vanilla JS, no dependencies. Talks JSON to the daemon. */
"use strict";

// ---------------------------------------------------------------- api --
const api = {
  async get(method) {
    const r = await fetch("/api/" + method);
    const j = await r.json();
    if (!j.ok) throw new Error((j.error || "Request failed") + (j.hint ? ` — ${j.hint}` : ""));
    return j.result;
  },
  async post(method, params) {
    const r = await fetch("/api/" + method, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params || {}),
    });
    const j = await r.json();
    if (!j.ok) throw new Error((j.error || "Request failed") + (j.hint ? ` — ${j.hint}` : ""));
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
  analysis: null,
  selectedFile: null,
  system: null,
  rt: null,
  hoverBand: -1,
  showGhosts: true,
  showSpectrum: true,
  busy: false,
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

function setBusy(value, text = "Working…") {
  S.busy = value;
  $("#busy-text").textContent = text;
  $("#busy-overlay").classList.toggle("hidden", !value);
}

async function withButton(button, work, label = "Working…") {
  if (!button || button.disabled) return;
  const old = button.innerHTML;
  button.disabled = true;
  button.innerHTML = `<span class="spinner"></span>${esc(label)}`;
  try { return await work(); }
  finally {
    button.disabled = false;
    // System actions may replace their own caption to reflect the new state.
    if (button.querySelector(".spinner")) button.innerHTML = old;
  }
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) return "";
  const units = ["B", "KB", "MB", "GB"];
  let n = bytes, i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i > 1 ? 1 : 0)} ${units[i]}`;
}

function closeSidebar() { document.body.classList.remove("sidebar-open"); }

function toast(msg, isError) {
  const el = document.createElement("div");
  el.className = "toast" + (isError ? " error" : "");
  el.textContent = msg;
  $("#toast-area").appendChild(el);
  setTimeout(() => {
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 220);
  }, isError ? 6500 : 3600);
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
  $("#dirty-dot").classList.toggle("visible", v);
  $("#revert-btn").disabled = !v;
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
  const requestedRange = $("#gain-range") ? $("#gain-range").value : "auto";
  GAIN_RANGE = requestedRange === "auto"
    ? clamp(Math.ceil((peak + 2) / 6) * 6, 12, 30)
    : parseInt(requestedRange, 10);
  const ymax = GAIN_RANGE;

  ctx.clearRect(0, 0, W, H);

  // grid
  ctx.font = "10px Inter, sans-serif";
  ctx.textAlign = "center";
  for (const dec of [1, 2, 3, 4]) {
    for (const m of [1, 2, 5]) {
      const f = m * Math.pow(10, dec);
      if (f > FMAX || f < FMIN) continue;
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
  if (S.showSpectrum && S.spectrum && S.spectrum.freqs && S.spectrum.freqs.length > 4) {
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
  if (S.showGhosts) for (let i = 0; i < filters.length; i++) {
    const f = filters[i];
    if (f.enabled === false) continue;
    const d = designBiquad(f.type || "peak", f.freq, f.gain_db || 0,
                           f.q || 1, SR_VIEW, f.use_q);
    ctx.strokeStyle = i === S.selectedBand ? "rgba(139,174,248,.85)"
                                             : "rgba(139,174,248,.35)";
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
  ctx.strokeStyle = "#9ce3b1"; ctx.lineWidth = 2.5; ctx.stroke();
  ctx.lineTo(freqToX(FMAX, W, padL, padR), dbToY(0, H, padT, padB, ymax));
  ctx.lineTo(freqToX(FMIN, W, padL, padR), dbToY(0, H, padT, padB, ymax));
  ctx.closePath();
  ctx.fillStyle = "rgba(156,227,177,.07)"; ctx.fill();
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
    ctx.fillStyle = sel ? "#8baef8" : (hov ? "#5d7fc4" : "#31415f");
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
    addBand(xToFreq(mx, W, padL, padR),
            Math.round(yToDb(my, H, padT, padB, GAIN_RANGE) * 10) / 10);
  });
  window.addEventListener("keydown", (e) => {
    const editing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName) ||
      document.activeElement.isContentEditable;
    if (e.key === "Delete" && S.selectedBand >= 0 && !editing) {
      currentFilters().splice(S.selectedBand, 1);
      S.selectedBand = -1;
      markDirty(); renderBands(); drawEQ(); updatePreamp();
    }
    if (e.key === " " && !editing && document.activeElement.tagName !== "BUTTON") {
      e.preventDefault(); toggleBypass();
    }
  });
}

// ----------------------------------------------------------- band table --
function addBand(freq = 1000, gain = 0) {
  const filters = currentFilters();
  if (!S.resolved) return;
  if (filters.length >= 64) { toast("A profile can contain at most 64 bands", true); return; }
  filters.push({ type: "peak", freq: Math.round(freq), gain_db: gain,
                 q: 1.0, enabled: true });
  S.selectedBand = filters.length - 1;
  markDirty(); renderBands(); drawEQ(); updatePreamp();
  const selected = $("#band-table tr.selected");
  if (selected) selected.scrollIntoView({ block: "nearest" });
}

function renderBands() {
  const tbody = $("#band-table tbody");
  tbody.innerHTML = "";
  const filters = currentFilters();
  $("#band-count").textContent = `${filters.length} band${filters.length === 1 ? "" : "s"}`;
  if (!filters.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="band-empty">No bands yet — double-click the curve or use Add band.</td></tr>';
  }
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
      <td><button class="delete-band" data-del="${i}" title="Remove band" aria-label="Remove band ${i + 1}">×</button></td>`;
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
    markDirty(); updatePreamp(); drawEQ(); renderChain();
  });
  chip.querySelector("#preamp-gain").addEventListener("change", (e) => {
    S.resolved.preamp = { ...(S.resolved.preamp || {}), mode: "manual",
                          gain_db: parseFloat(e.target.value) || 0 };
    markDirty(); updatePreamp(); drawEQ();
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
  let shown = 0;
  for (const p of S.profiles) {
    const tags = p.tags || [];
    const opt = document.createElement("option");
    opt.value = p.id; opt.textContent = p.name;
    if (p.id === S.currentId) opt.selected = true;
    select.appendChild(opt);
    if (q && !(p.name + p.id + tags.join()).toLowerCase().includes(q)) continue;
    shown++;
    const item = document.createElement("div");
    item.className = "profile-item" + (p.id === S.currentId ? " selected" : "");
    item.setAttribute("role", "button");
    item.tabIndex = 0;
    item.innerHTML = `<div class="pname">${esc(p.name)}</div>
      <div class="pmeta"><span class="origin-badge">${p.builtin ? "Preset" : "Custom"}</span>${tags.length ? " · " + esc(tags.slice(0, 2).join(", ")) : ""}</div>
      ${p.id === S.currentId ? '<svg class="active-check"><use href="#i-check"/></svg>' : ""}`;
    item.addEventListener("click", () => activateProfile(p.id));
    item.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); activateProfile(p.id); }
    });
    list.appendChild(item);
  }
  $("#profile-count").textContent = q ? `${shown}/${S.profiles.length}` : S.profiles.length;
  if (!shown) list.innerHTML = '<div class="empty-state compact"><p>No profiles match your search.</p></div>';
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
    $("#profile-title").textContent = S.resolved.name || id;
    const meta = S.profiles.find(p => p.id === id) || {};
    $("#profile-description").textContent = S.resolved.description ||
      `${meta.builtin ? "Factory preset" : "Custom profile"} · ${currentFilters().length} EQ bands · ready to shape`;
    $("#profile-select").value = id;
    closeSidebar();
    renderBands(); renderChain(); drawEQ(); updatePreamp();
    await loadProfiles();
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
    if (!name) { toast("Enter a display name", true); return; }
    if (S.profiles.some(p => p.id === id)) {
      toast(`A profile named “${id}” already exists — choose another ID`, true); return;
    }
  }
  const prof = { ...JSON.parse(JSON.stringify(r)), id, name, builtin: false };
  delete prof.exported_from; delete prof.exported_at;
  try {
    const saved = await api.post("save_profile", { profile: prof });
    await api.post("set_profile", { profile: id });
    toast("Saved and activated “" + name + "”");
    if (saved.warnings && saved.warnings.length)
      toast(saved.warnings.join(" · "), true);
    await loadProfiles();
    await activateProfile(id, true);
  } catch (e) { toast(String(e.message), true); }
}

function modal(title, fields = [], options = {}) {
  return new Promise((resolve) => {
    const back = $("#modal-backdrop");
    $("#modal-title").textContent = title;
    $("#modal-eyebrow").textContent = options.eyebrow || "EQFORGE";
    const body = $("#modal-body");
    body.innerHTML = (options.description ? `<p>${esc(options.description)}</p>` : "") + (options.bodyHtml || "");
    const inputs = [];
    for (const f of fields) {
      const wrap = document.createElement("div");
      wrap.className = "modal-field";
      const label = document.createElement("label");
      label.textContent = f.title || f.label;
      const inp = document.createElement("input");
      inp.name = f.label; inp.placeholder = f.placeholder || f.label;
      inp.value = f.value ?? ""; inp.type = f.type || "text";
      if (f.required) inp.required = true;
      label.htmlFor = inp.id = `modal-field-${f.label}`;
      wrap.append(label, inp);
      if (f.help) { const small = document.createElement("small"); small.className = "fine-print"; small.textContent = f.help; wrap.appendChild(small); }
      body.appendChild(wrap); inputs.push([f.label, inp]);
    }
    const ok = $("#modal-ok");
    ok.textContent = options.okText || "Confirm";
    ok.classList.toggle("danger", !!options.danger);
    ok.classList.toggle("primary", !options.danger);
    back.classList.remove("hidden");
    inputs[0] && inputs[0][1].focus();
    const done = (val) => {
      back.classList.add("hidden");
      ok.onclick = null; $("#modal-cancel").onclick = null; $("#modal-close").onclick = null;
      back.onclick = null; document.removeEventListener("keydown", onKey);
      resolve(val);
    };
    const onKey = (e) => {
      if (e.key === "Escape") done(null);
      if (e.key === "Enter" && e.target.tagName === "INPUT") ok.click();
    };
    ok.onclick = () => done(Object.fromEntries(inputs.map(([k, i]) => [k, i.value])));
    $("#modal-cancel").onclick = () => done(null);
    $("#modal-close").onclick = () => done(null);
    back.onclick = (e) => { if (e.target === back) done(null); };
    document.addEventListener("keydown", onKey);
  });
}

// ---------------------------------------------------- profile file tools --
async function importProfileFile(file) {
  if (!file) return;
  const button = $("#import-btn");
  await withButton(button, async () => {
    try {
      const content = await file.text();
      const r = await api.post("import_profile", { filename: file.name, content });
      await loadProfiles();
      await activateProfile(r.id, true);
      toast(`Imported ${r.filters} bands as “${r.name}”`);
    } catch (e) { toast(e.message, true); }
  }, "Importing…");
}

async function exportCurrentProfile() {
  if (!S.currentId) return;
  try {
    const r = await api.post("export_profile", { profile: S.currentId });
    const blob = new Blob([r.content], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url; link.download = r.filename;
    document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast(`Exported ${r.filename}`);
  } catch (e) { toast(e.message, true); }
}

async function duplicateCurrentProfile() {
  if (!S.resolved) return;
  const res = await modal("Duplicate profile", [
    { label: "id", title: "New profile ID", value: `${S.currentId}-copy` },
    { label: "name", title: "Display name", value: `${S.resolved.name || S.currentId} (copy)` },
  ], { description: "Create an independent, fully editable copy.", okText: "Create copy" });
  if (!res) return;
  const id = res.id.trim(), name = res.name.trim();
  if (!/^[a-zA-Z0-9][a-zA-Z0-9._-]*$/.test(id) || !name) {
    toast("Enter a name and a valid ID (letters, digits, . _ -)", true); return;
  }
  if (S.profiles.some(p => p.id === id)) {
    toast(`A profile named “${id}” already exists — choose another ID`, true); return;
  }
  const prof = { ...JSON.parse(JSON.stringify(S.resolved)), id, name, builtin: false };
  delete prof.exported_from; delete prof.exported_at; delete prof.extends;
  try {
    await api.post("save_profile", { profile: prof });
    await loadProfiles(); await activateProfile(id, true);
    toast(`Created “${name}”`);
  } catch (e) { toast(e.message, true); }
}

async function deleteCurrentProfile() {
  if (!S.currentId) return;
  const current = S.profiles.find(p => p.id === S.currentId);
  if (current && current.builtin) { toast("Factory presets are read-only. Duplicate one to customize it.", true); return; }
  const name = (S.resolved || {}).name || S.currentId;
  const res = await modal("Delete this profile?", [], {
    description: `“${name}” will be removed from your library. This cannot be undone.`,
    okText: "Delete profile", danger: true, eyebrow: "DANGER ZONE",
  });
  if (!res) return;
  try {
    const r = await api.post("delete_profile", { profile: S.currentId });
    S.currentId = null; S.resolved = null; S.dirty = false;
    await loadProfiles(); await activateProfile(r.active_profile || "flat", true);
    toast(`Deleted “${name}”`);
  } catch (e) { toast(e.message, true); }
}

// ---------------------------------------------------------- analysis --
async function analyzeFile() {
  const path = $("#file-path").value.trim();
  if (!S.selectedFile && !path) { toast("Choose an audio file or enter a path first", true); return; }
  const box = $("#analysis-result");
  box.innerHTML = '<div class="loading-row"><span class="spinner"></span>Measuring loudness and spectrum…</div>';
  $("#suggest-controls").classList.add("hidden");
  await withButton($("#analyze-btn"), async () => {
    try {
      let r;
      if (S.selectedFile) {
        if (S.selectedFile.size > 128 * 1024 * 1024) throw new Error("Audio uploads are limited to 128 MB");
        const dataUrl = await new Promise((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(reader.result);
          reader.onerror = () => reject(new Error("Could not read the selected file"));
          reader.readAsDataURL(S.selectedFile);
        });
        r = await api.post("analyze_upload", {
          filename: S.selectedFile.name, data: String(dataUrl).split(",", 2)[1],
          profile: S.currentId,
        });
      } else {
        r = await api.post("analyze_file", { path, profile: S.currentId });
      }
      S.analysis = r;
      renderAnalysis(r);
    } catch (e) {
      box.innerHTML = `<div class="finding warn"><div class="fmsg">${esc(e.message)}</div></div>`;
    }
  }, "Analyzing…");
}

function renderAnalysis(r) {
  const rep = r.report;
  const box = $("#analysis-result");
  S.spectrum = rep.spectrum && rep.spectrum.freqs ? rep.spectrum : null;
  drawEQ();
  const fmt = (v, unit = "", digits = 1) =>
    v === null || v === undefined || (typeof v === "number" && !Number.isFinite(v)) ? "—" :
      (typeof v === "number" ? v.toFixed(digits) : v) + " " + unit;
  const advice = r.advice || { findings: [] };
  const canApply = advice.suggested_preamp_db !== null && advice.suggested_preamp_db !== undefined;
  const suggested = advice.suggested_filters || [];
  box.innerHTML = `
    <div class="ar-section"><h4>${esc(r.path.split("/").pop())}</h4>
      <div class="ar-grid">
        <span class="k">Integrated</span><span class="v">${fmt(rep.integrated_lufs, "LUFS")}</span>
        <span class="k">Loudness range</span><span class="v">${fmt(rep.loudness_range_lu, "LU")}</span>
        <span class="k">Sample peak</span><span class="v">${fmt(rep.peak_db, "dBFS", 2)}</span>
        <span class="k">True peak</span><span class="v">${fmt(rep.true_peak_db, "dBTP", 2)}</span>
        <span class="k">Crest factor</span><span class="v">${fmt(rep.crest_factor_db, "dB")}</span>
        <span class="k">Duration</span><span class="v">${fmt(rep.duration_s, "s", 1)}</span>
        <span class="k">Sample rate</span><span class="v">${fmt(rep.sample_rate, "Hz", 0)}</span>
        <span class="k">Channels</span><span class="v">${fmt(rep.channels, "", 0)}</span>
      </div></div>
    <canvas class="spectrum-canvas" id="spec-canvas"></canvas>
    ${canApply || suggested.length ? `<div class="ar-section"><h4 class="section-title">Quick fixes</h4>
      ${canApply ? `<button id="apply-preamp" class="btn small full-width">Use safe preamp (${Number(advice.suggested_preamp_db).toFixed(1)} dB)</button>` : ""}
      ${suggested.length ? `<button id="apply-fixes" class="btn small full-width" style="margin-top:6px">Add ${suggested.length} recommended band${suggested.length === 1 ? "" : "s"}</button>` : ""}
    </div>` : ""}
    <div class="ar-section"><h4 class="section-title">Smart advisor</h4>${
      advice.findings.map(f => `
      <div class="finding ${esc(f.severity)}">
        <div class="fmsg">${esc(f.message)}</div>
        ${f.hint ? `<div class="fhint">${esc(f.hint)}</div>` : ""}
      </div>`).join("")}</div>`;
  $("#suggest-controls").classList.toggle("hidden", !S.spectrum);
  if (canApply) $("#apply-preamp").addEventListener("click", () => {
    S.resolved.preamp = { mode: "manual", gain_db: advice.suggested_preamp_db };
    markDirty(); renderChain(); updatePreamp(); drawEQ();
    toast("Safe preamp applied to the preview — save to activate it");
  });
  if (suggested.length) $("#apply-fixes").addEventListener("click", () => {
    currentFilters().push(...JSON.parse(JSON.stringify(suggested)));
    markDirty(); renderBands(); updatePreamp(); drawEQ();
    toast("Recommended bands added to the preview");
  });
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
  ctx.strokeStyle = "#8baef8"; ctx.beginPath();
  for (let i = 0; i < freqs.length; i++) {
    const x = (Math.log10(freqs[i]) - Math.log10(20)) /
              (Math.log10(20000) - Math.log10(20)) * W;
    const y = H - (db[i] - lo) / (hi - lo + 1e-9) * (H - 8) - 4;
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  }
  ctx.stroke();
}

async function generateSuggestedEQ() {
  if (!S.spectrum) { toast("Analyze an audio file first", true); return; }
  await withButton($("#suggest-btn"), async () => {
    try {
      const target = $("#target-select").value;
      const r = await api.post("suggest_eq", { spectrum: S.spectrum, target,
                                               max_gain: 6, max_filters: 8 });
      const filters = r.suggested_filters || [];
      if (!filters.length) { toast("This content is already close to the selected target"); return; }
      currentFilters().push(...filters);
      markDirty(); renderBands(); updatePreamp(); drawEQ();
      toast(`Added ${filters.length} corrective bands · RMS deviation ${r.deviation.rms_deviation_db.toFixed(1)} dB`);
    } catch (e) { toast(e.message, true); }
  }, "Matching…");
}

// ------------------------------------------------------------ system --
async function loadDevices() {
  try {
    const r = await api.get("devices");
    const select = $("#device-select");
    select.innerHTML = "";
    select.disabled = !r.available;
    $("#pw-mini-status").textContent = r.available ? "Ready" : "Unavailable";
    if (!r.available) {
      select.innerHTML = "<option>PipeWire not available</option>";
      $("#device-info").textContent = "Install and start PipeWire to manage system audio.";
      $("#dot-pw").classList.remove("ok");
      return;
    }
    const sinks = r.devices.filter(x => x.direction === "sink");
    for (const d of sinks) {
      const opt = document.createElement("option");
      opt.value = d.node_name;
      opt.textContent = `${d.description} · ${d.kind}`;
      if (d.node_name === r.default_sink) opt.selected = true;
      select.appendChild(opt);
    }
    if (!sinks.length) select.innerHTML = "<option>No output devices found</option>";
    const cur = sinks.find(d => d.node_name === r.default_sink) || sinks[0];
    $("#device-info").textContent = cur ? `${cur.kind} · ${cur.media_class}` : "";
    $("#dot-pw").classList.add("ok");
  } catch {
    $("#dot-pw").classList.remove("ok");
    $("#pw-mini-status").textContent = "Unavailable";
  }
}

async function loadStreams() {
  try {
    const r = await api.get("streams");
    const el = $("#stream-list");
    $("#stream-count").textContent = r.streams.length;
    if (!r.streams.length) { el.innerHTML = '<div class="empty-line">Play audio in an app to see its stream here.</div>'; return; }
    el.innerHTML = r.streams.slice(0, 12).map(s =>
      `<div class="row"><span>${esc(s.name.slice(0, 26))}</span>
       <span class="muted">${esc(s.binary || "")}</span></div>`).join("");
  } catch { /* ignore */ }
}

async function loadSystemOverview() {
  try {
    const r = await api.get("system_overview");
    S.system = r;
    $("#auto-wire").checked = !!r.auto_wire;
    const services = r.autostart || [];
    const enabled = services.length > 0 && services.every(s => s.enabled === "enabled");
    const active = services.length > 0 && services.every(s => s.active === "active");
    $("#autostart-info").textContent = !services.some(s => s.installed)
      ? "Disabled · EQForge only runs when you open it"
      : `${enabled ? "Enabled" : "Installed, not enabled"} · ${active ? "services running" : "services not running"}`;
    $("#autostart-info").className = "status-description " + (enabled ? "ok" : "");
    $("#autostart-btn").textContent = enabled ? "Disable login autostart" : "Enable login autostart";
    const always = r.always_on || {};
    $("#always-on-info").textContent = always.ok
      ? `Active · new streams start on ${always.node}`
      : always.installed
        ? `Installed · waiting for ${always.node || "the EQForge node"}`
        : "Disabled · streams are connected after they appear";
    $("#always-on-info").className = "status-description " + (always.ok ? "ok" : always.installed ? "warn" : "");
    $("#always-on-btn").textContent = always.installed ? "Disable always-on routing" : "Enable always-on routing";
    if (r.native && r.native.available)
      $("#engine-badge").textContent = `Native DSP · v${r.native.version || "1"}`;
    else if (r.native) $("#engine-badge").textContent = "NumPy DSP fallback";
  } catch (e) {
    $("#autostart-info").textContent = "System details are unavailable";
  }
}

async function startProcessing() {
  await withButton($("#rt-start"), async () => {
    try {
      const r = await api.post("start_rt", { channels: 2 });
      toast(r.already_running ? "Real-time processing is already running" : `Real-time engine started · PID ${r.pid}`);
      await refreshStatus(); await loadSystemOverview(); await loadDevices();
    } catch (e) { toast(e.message, true); }
  }, "Starting…");
  renderMeters(S.rt);
}

async function stopProcessing() {
  await withButton($("#rt-stop"), async () => {
    try {
      await api.post("stop_rt", {});
      toast("Real-time processing stopped");
      await refreshStatus(); await loadSystemOverview();
    } catch (e) { toast(e.message, true); }
  }, "Stopping…");
  renderMeters(S.rt);
}

async function wireStreams() {
  await withButton($("#rt-wire"), async () => {
    try {
      const r = await api.post("wire", {});
      toast(`Connected ${r.streams_moved} stream${r.streams_moved === 1 ? "" : "s"} · ${r.already_wired} already connected`);
      await loadStreams();
    } catch (e) { toast(e.message, true); }
  }, "Connecting…");
}

async function toggleAutostart() {
  const enabled = $("#autostart-btn").textContent.startsWith("Disable");
  if (enabled) {
    const res = await modal("Disable login autostart?", [], {
      description: "EQForge will no longer start its engine and background services when you log in.",
      okText: "Disable autostart", danger: true,
    });
    if (!res) return;
  }
  await withButton($("#autostart-btn"), async () => {
    try {
      const r = await api.post("autostart", { value: !enabled });
      toast(!enabled
        ? (r.complete ? "Login autostart enabled" : "Service files created; systemd needs attention")
        : "Login autostart disabled", !r.complete);
      await loadSystemOverview();
    } catch (e) { toast(e.message, true); }
  }, enabled ? "Disabling…" : "Enabling…");
}

async function toggleAlwaysOn() {
  const enabled = $("#always-on-btn").textContent.startsWith("Disable");
  await withButton($("#always-on-btn"), async () => {
    try {
      const r = await api.post("always_on", { value: !enabled });
      toast(!enabled
        ? (r.restarted ? "Always-on routing enabled" : "Routing saved; WirePlumber restart needs attention")
        : "Always-on routing disabled", !!r.error);
      await loadSystemOverview();
    } catch (e) { toast(e.message, true); }
  }, enabled ? "Disabling…" : "Enabling…");
}

async function setupIntegration() {
  await withButton($("#setup-btn"), async () => {
    try {
      const r = await api.post("system_setup", {});
      const updated = r.files.filter(f => f.status !== "same").length;
      toast(updated ? `Integration repaired · ${updated} file${updated === 1 ? "" : "s"} updated` : "Integration files are already up to date");
      if (!r.daemon_reload) toast("Files saved, but systemd could not reload them", true);
      await loadSystemOverview();
    } catch (e) { toast(e.message, true); }
  }, "Installing…");
}

async function runDoctor() {
  const box = $("#doctor-result");
  box.innerHTML = '<div class="loading-row"><span class="spinner"></span>Running checks…</div>';
  await withButton($("#doctor-btn"), async () => {
    try {
      const r = await api.post("doctor", {});
      box.innerHTML = r.checks.map(c => `<div class="doctor-check ${c.status === "PASS" ? "" : "warn"}">
        <svg><use href="#${c.status === "PASS" ? "i-check" : "i-help"}"/></svg>
        <div><strong>${esc(c.name)}</strong><small>${esc(c.detail)}${c.hint ? " · " + esc(c.hint) : ""}</small></div></div>`).join("");
      toast(r.healthy ? "All diagnostics passed" : `Diagnostics complete · ${r.warnings} item${r.warnings === 1 ? "" : "s"} need attention`, !r.healthy);
    } catch (e) { box.innerHTML = `<div class="finding warn">${esc(e.message)}</div>`; }
  }, "Checking…");
}

async function showLogs() {
  try {
    const r = await api.get("runtime_log");
    const text = ["REAL-TIME ENGINE", ...(r.rt.length ? r.rt : ["No engine log entries."]),
                  "", "BACKGROUND SERVICE", ...(r.daemon.length ? r.daemon : ["No daemon log entries."])].join("\n");
    await modal("Recent engine logs", [], {
      description: "The latest diagnostics from this computer.", okText: "Done",
      bodyHtml: `<div class="log-view">${esc(text)}</div>`,
    });
  } catch (e) { toast(e.message, true); }
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
      `Running · ${rt.sample_rate || "—"} Hz · ${rt.buffer_size || "—"} frame buffer`;
    $("#system-headline").textContent = "Processing is live";
    $("#system-state-dot").className = "large-status-dot ok";
    $("#rt-start").disabled = true;
    $("#rt-stop").disabled = false;
    const peak = rt.out_peak_db ?? -70;
    $("#m-peak").textContent = `${peak <= -69 ? "−∞" : peak.toFixed(1)} dBFS`;
    drawOutputMeter(peak);
  } else {
    dot.classList.remove("ok");
    $("#rt-info").textContent = "Stopped · start processing to hear your profile system-wide.";
    $("#system-headline").textContent = "Engine is stopped";
    $("#system-state-dot").className = "large-status-dot";
    $("#rt-start").disabled = false;
    $("#rt-stop").disabled = true;
    $("#m-lufs").textContent = "— LUFS";
    $("#m-lim").textContent = "—";
    $("#m-peak").textContent = "— dBFS";
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
  grad.addColorStop(0, "#9ce3b1"); grad.addColorStop(0.75, "#efc477");
  grad.addColorStop(0.95, "#fb8b9d");
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
  es.addEventListener("system_changed", () => loadSystemOverview());
  es.addEventListener("auto_wire", (e) => {
    const d = JSON.parse(e.data); $("#auto-wire").checked = !!d.enabled;
  });
  es.addEventListener("rt_error", (e) => {
    const d = JSON.parse(e.data); toast(d.hint ? `${d.error} · ${d.hint}` : d.error, true);
  });
  es.onerror = () => { $("#dot-daemon").classList.remove("ok"); };
  es.onopen = () => { $("#dot-daemon").classList.add("ok"); };
}

function selectAudioFile(file) {
  if (!file) return;
  S.selectedFile = file;
  $("#file-path").value = "";
  const el = $("#selected-file");
  el.textContent = `${file.name} · ${formatBytes(file.size)}`;
  el.classList.remove("hidden");
}

async function showHelp() {
  await modal("Studio shortcuts", [], {
    eyebrow: "QUICK GUIDE", okText: "Got it",
    description: "Everything is available here in the GUI — no command line needed.",
    bodyHtml: `<div class="shortcut-list">
      <span>Save and activate the current profile</span><span><kbd>Ctrl</kbd> + <kbd>S</kbd></span>
      <span>Bypass / resume all processing</span><span><kbd>Space</kbd></span>
      <span>Remove the selected EQ band</span><span><kbd>Delete</kbd></span>
      <span>Add a band at a precise point</span><span>Double-click curve</span>
      <span>Adjust a band's Q / width</span><span>Scroll over handle</span>
      <span>Open this guide</span><span><kbd>?</kbd></span>
    </div><p>Tip: analysis and target matching only change your preview. Use <strong>Save & activate</strong> when the curve sounds right.</p>`,
  });
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
    $("#slot-a-name").textContent = st.slot_a;
    $("#slot-b-name").textContent = st.slot_b;
    $("#bypass-btn").classList.toggle("on", st.bypass);
    $("#bypass-btn").setAttribute("aria-pressed", String(!!st.bypass));
    $("#auto-switch").checked = !!st.auto_switch;
    $("#auto-wire").checked = !!st.auto_wire;
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
  $("#add-band-btn").addEventListener("click", () => addBand());
  $("#show-ghosts").addEventListener("change", (e) => { S.showGhosts = e.target.checked; drawEQ(); });
  $("#show-spectrum").addEventListener("change", (e) => { S.showSpectrum = e.target.checked; drawEQ(); });
  $("#gain-range").addEventListener("change", drawEQ);
  $("#help-btn").addEventListener("click", showHelp);
  $("#menu-toggle").addEventListener("click", () => document.body.classList.add("sidebar-open"));
  $("#sidebar-close").addEventListener("click", closeSidebar);
  $("#sidebar-backdrop").addEventListener("click", closeSidebar);
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
    const newId = res.id.trim();
    if (!/^[a-zA-Z0-9][a-zA-Z0-9._-]*$/.test(newId)) {
      toast("Invalid ID — use letters, digits, . _ or -", true); return;
    }
    if (S.profiles.some(p => p.id === newId)) {
      toast(`A profile named “${newId}” already exists`, true); return;
    }
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
  $("#import-btn").addEventListener("click", () => $("#import-input").click());
  $("#import-input").addEventListener("change", async (e) => {
    await importProfileFile(e.target.files[0]); e.target.value = "";
  });
  $("#duplicate-btn").addEventListener("click", duplicateCurrentProfile);
  $("#export-btn").addEventListener("click", exportCurrentProfile);
  $("#delete-btn").addEventListener("click", deleteCurrentProfile);
  $("#file-input").addEventListener("change", (e) => selectAudioFile(e.target.files[0]));
  const drop = $("#file-drop");
  ["dragenter", "dragover"].forEach(type => drop.addEventListener(type, (e) => {
    e.preventDefault(); drop.classList.add("dragging");
  }));
  ["dragleave", "drop"].forEach(type => drop.addEventListener(type, (e) => {
    e.preventDefault(); drop.classList.remove("dragging");
  }));
  drop.addEventListener("drop", (e) => selectAudioFile(e.dataTransfer.files[0]));
  $("#analyze-btn").addEventListener("click", analyzeFile);
  $("#suggest-btn").addEventListener("click", generateSuggestedEQ);
  $("#file-path").addEventListener("input", (e) => {
    if (e.target.value.trim()) { S.selectedFile = null; $("#selected-file").classList.add("hidden"); }
  });
  $("#file-path").addEventListener("keydown", (e) => {
    if (e.key === "Enter") analyzeFile(); });
  document.querySelectorAll(".tab").forEach((t) =>
    t.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
      document.querySelectorAll(".tab-body").forEach(x => x.classList.remove("active"));
      t.classList.add("active");
      $("#tab-" + t.dataset.tab).classList.add("active");
      if (t.dataset.tab === "system") { loadDevices(); loadStreams(); loadSystemOverview(); }
    }));
  $("#auto-switch").addEventListener("change", async (e) => {
    try {
      await api.post("auto_switch", { value: e.target.checked });
      if (e.target.checked) await api.post("apply_rules_now", {});
      toast(e.target.checked ? "Device-based profiles enabled" : "Device-based profiles disabled");
    } catch (err) { toast(err.message, true); e.target.checked = !e.target.checked; }
  });
  $("#auto-wire").addEventListener("change", async (e) => {
    try {
      await api.post("set_auto_wire", { value: e.target.checked });
      toast(e.target.checked ? "Automatic stream wiring enabled" : "Automatic stream wiring disabled");
    } catch (err) { toast(err.message, true); e.target.checked = !e.target.checked; }
  });
  $("#device-select").addEventListener("change", async (e) => {
    try {
      const r = await api.post("set_device", { device: e.target.value });
      toast(`Default output changed to ${r.description}`);
      await loadDevices();
    } catch (err) { toast(err.message, true); await loadDevices(); }
  });
  $("#rt-start").addEventListener("click", startProcessing);
  $("#rt-stop").addEventListener("click", stopProcessing);
  $("#rt-wire").addEventListener("click", wireStreams);
  $("#rt-refresh").addEventListener("click", async () => {
    await Promise.all([refreshStatus(), loadDevices(), loadStreams(), loadSystemOverview()]);
    toast("System status refreshed");
  });
  $("#autostart-btn").addEventListener("click", toggleAutostart);
  $("#always-on-btn").addEventListener("click", toggleAlwaysOn);
  $("#setup-btn").addEventListener("click", setupIntegration);
  $("#doctor-btn").addEventListener("click", runDoctor);
  $("#logs-btn").addEventListener("click", showLogs);

  window.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
      e.preventDefault(); saveProfile(false);
    } else if (e.key === "?" && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) {
      e.preventDefault(); showHelp();
    } else if (e.key === "Escape") closeSidebar();
  });
  window.addEventListener("beforeunload", (e) => {
    if (S.dirty) { e.preventDefault(); e.returnValue = ""; }
  });
  window.addEventListener("resize", () => { drawEQ(); drawMiniSpectrum(); });

  connectSSE();
  (async () => {
    await loadProfiles();
    await refreshStatus();
    await Promise.all([loadDevices(), loadSystemOverview()]);
    if (S.system && S.system.native) {
      $("#engine-badge").textContent = S.system.native.available
        ? `Native DSP · ${S.system.native.version || "ready"}` : "NumPy DSP fallback";
    }
  })();
  setInterval(refreshStatus, 5000);
  setInterval(() => {
    if ($("#tab-system").classList.contains("active")) loadStreams();
  }, 4000);
}

document.addEventListener("DOMContentLoaded", init);
