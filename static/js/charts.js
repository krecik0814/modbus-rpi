// Wykresy na canvas: obszar z gradientem, przerwy dla braków danych, podpowiedź pod kursorem.
// Bez zależności; ostre na ekranach HiDPI (devicePixelRatio).

import { fmt, fmtTime, fmtDateTime } from './core.js';

const GRID = '#1e293b';
const LABEL = '#64748b';

/**
 * new AreaChart(canvas, {color, decimals, unit, axes: false, markers: false})
 * chart.setData([[ts, value|null], ...])   // ts w sekundach
 */
export class AreaChart {
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.opts = { color: '#3b82f6', decimals: 2, unit: '', axes: false, markers: false, ...opts };
    this.data = [];
    this.hover = null;
    this.tip = null;
    this._ro = new ResizeObserver(() => this.draw());
    this._ro.observe(canvas);
    this._move = (e) => this._onMove(e);
    this._leave = () => { this.hover = null; this._hideTip(); this.draw(); };
    canvas.addEventListener('pointermove', this._move);
    canvas.addEventListener('pointerleave', this._leave);
  }

  setData(points) {
    this.data = points || [];
    this.draw();
  }

  destroy() {
    this._ro.disconnect();
    this.canvas.removeEventListener('pointermove', this._move);
    this.canvas.removeEventListener('pointerleave', this._leave);
    this._hideTip();
  }

  stats() {
    const nums = this.data.map((p) => p[1]).filter((v) => v != null && Number.isFinite(v));
    if (!nums.length) return null;
    let mn = Infinity, mx = -Infinity, sum = 0;
    for (const v of nums) { if (v < mn) mn = v; if (v > mx) mx = v; sum += v; }
    return { min: mn, max: mx, avg: sum / nums.length, count: nums.length, last: nums[nums.length - 1] };
  }

  _layout() {
    const rect = this.canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const W = Math.max(1, Math.round(rect.width * dpr));
    const H = Math.max(1, Math.round(rect.height * dpr));
    if (this.canvas.width !== W || this.canvas.height !== H) {
      this.canvas.width = W;
      this.canvas.height = H;
    }
    const a = this.opts.axes;
    const m = a ? { l: 64 * dpr, r: 16 * dpr, t: 14 * dpr, b: 28 * dpr } : { l: 0, r: 0, t: 6 * dpr, b: 0 };
    return { W, H, dpr, m, cw: W - m.l - m.r, ch: H - m.t - m.b };
  }

  draw() {
    const L = this._layout();
    const { W, H, dpr, m, cw, ch } = L;
    const ctx = this.canvas.getContext('2d');
    ctx.clearRect(0, 0, W, H);
    this._geom = null;
    const pts = this.data.filter((p) => p[1] != null && Number.isFinite(p[1]));
    if (pts.length < 2) {
      if (this.opts.axes) {
        ctx.fillStyle = LABEL;
        ctx.font = `${12 * dpr}px system-ui, sans-serif`;
        ctx.textAlign = 'center';
        ctx.fillText(pts.length ? 'Za mało danych' : 'Brak danych w tym zakresie', W / 2, H / 2);
      }
      return;
    }
    const t0 = this.data[0][0], t1 = this.data[this.data.length - 1][0];
    const span = Math.max(1e-9, t1 - t0);
    let mn = Infinity, mx = -Infinity;
    for (const p of pts) { if (p[1] < mn) mn = p[1]; if (p[1] > mx) mx = p[1]; }
    const range = mx - mn || Math.abs(mn) * 0.01 || 1;
    const pad = range * (this.opts.axes ? 0.12 : 0.18);
    const yMin = mn - pad, yMax = mx + pad;
    const px = (t) => m.l + ((t - t0) / span) * cw;
    const py = (v) => m.t + ch - ((v - yMin) / (yMax - yMin)) * ch;
    this._geom = { px, py, m, ch, dpr };

    // przerwa, gdy brak wartości albo nagły skok odstępu czasu względem lokalnego rytmu próbek
    // (dane z SQLite co 60 s i z pamięci co 1 s łączą się w jedną linię)
    const steps = [];
    for (let i = 1; i < this.data.length; i++) steps.push(this.data[i][0] - this.data[i - 1][0]);
    const sorted = [...steps].sort((a, b) => a - b);
    const median = sorted[Math.floor(sorted.length / 2)] || 1;
    const segments = [];
    let seg = [];
    for (let i = 0; i < this.data.length; i++) {
      const [t, v] = this.data[i];
      const dt = i ? steps[i - 1] : 0;
      const prevDt = i > 1 ? steps[i - 2] : dt;
      const gap = i > 0 && dt > 3.5 * median && dt > 2.5 * prevDt;
      if (v == null || !Number.isFinite(v) || gap) {
        if (seg.length) segments.push(seg);
        seg = [];
        if (v == null || !Number.isFinite(v)) continue;
      }
      seg.push([px(t), py(v)]);
    }
    if (seg.length) segments.push(seg);

    if (this.opts.axes) this._axes(ctx, L, yMin, yMax, t0, t1, px, py);

    const color = this.opts.color;
    const grad = ctx.createLinearGradient(0, m.t, 0, m.t + ch);
    grad.addColorStop(0, color + '55');
    grad.addColorStop(1, color + '05');
    const curve = (s, cont = false) => {
      if (cont) ctx.lineTo(s[0][0], s[0][1]); else ctx.moveTo(s[0][0], s[0][1]);
      for (let i = 1; i < s.length; i++) {
        const [x0, y0] = s[i - 1], [x1, y1] = s[i];
        const cx = (x0 + x1) / 2;
        ctx.bezierCurveTo(cx, y0, cx, y1, x1, y1);
      }
    };
    for (const s of segments) {
      if (s.length === 1) {
        ctx.beginPath();
        ctx.arc(s[0][0], s[0][1], 2 * dpr, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.fill();
        continue;
      }
      ctx.beginPath();
      ctx.moveTo(s[0][0], m.t + ch);
      curve(s, true);
      ctx.lineTo(s[s.length - 1][0], m.t + ch);
      ctx.closePath();
      ctx.fillStyle = grad;
      ctx.fill();
      ctx.beginPath();
      curve(s);
      ctx.strokeStyle = color;
      ctx.lineWidth = 2 * dpr;
      ctx.lineJoin = 'round';
      ctx.stroke();
    }

    if (this.opts.markers) {
      const st = this.stats();
      const dot = (p, c, r) => { ctx.beginPath(); ctx.arc(px(p[0]), py(p[1]), r * dpr, 0, Math.PI * 2); ctx.fillStyle = c; ctx.fill(); };
      const pMin = pts.find((p) => p[1] === st.min), pMax = pts.find((p) => p[1] === st.max);
      // średnia
      ctx.setLineDash([6 * dpr, 4 * dpr]);
      ctx.strokeStyle = '#eab30877';
      ctx.lineWidth = 1.5 * dpr;
      ctx.beginPath(); ctx.moveTo(m.l, py(st.avg)); ctx.lineTo(m.l + cw, py(st.avg)); ctx.stroke();
      ctx.setLineDash([]);
      dot(pMin, '#06b6d4', 4);
      dot(pMax, '#ef4444', 4);
      const last = pts[pts.length - 1];
      dot(last, color, 5);
      dot(last, '#fff', 2.5);
    }

    if (this.hover) this._drawHover(ctx);
  }

  _axes(ctx, L, yMin, yMax, t0, t1, px, py) {
    const { W, H, dpr, m, cw, ch } = L;
    ctx.font = `${11 * dpr}px Consolas, ui-monospace, monospace`;
    ctx.lineWidth = 1;
    ctx.strokeStyle = GRID;
    ctx.fillStyle = LABEL;
    ctx.textAlign = 'right';
    const steps = 5;
    const dec = Math.max(0, Math.min(4, this.opts.decimals));
    for (let i = 0; i <= steps; i++) {
      const v = yMin + ((yMax - yMin) * i) / steps;
      const y = py(v);
      ctx.beginPath(); ctx.moveTo(m.l, y); ctx.lineTo(m.l + cw, y); ctx.stroke();
      ctx.fillText(fmt(v, dec), m.l - 8 * dpr, y + 4 * dpr);
    }
    ctx.textAlign = 'center';
    const span = t1 - t0;
    const n = Math.max(2, Math.min(8, Math.floor(cw / (110 * dpr))));
    const long = span > 36 * 3600;
    for (let i = 0; i <= n; i++) {
      const t = t0 + (span * i) / n;
      const x = px(t);
      ctx.beginPath(); ctx.moveTo(x, m.t); ctx.lineTo(x, m.t + ch); ctx.stroke();
      const d = new Date(t * 1000);
      const label = long
        ? d.toLocaleDateString('pl-PL', { day: '2-digit', month: '2-digit' }) + ' ' + d.toLocaleTimeString('pl-PL', { hour: '2-digit', minute: '2-digit' })
        : d.toLocaleTimeString('pl-PL', span > 3 * 3600 ? { hour: '2-digit', minute: '2-digit' } : undefined);
      ctx.fillText(label, Math.min(Math.max(x, m.l + 30 * dpr), W - 30 * dpr), H - 8 * dpr);
    }
  }

  _nearest(xCss) {
    if (!this._geom || !this.data.length) return null;
    const { px, dpr } = this._geom;
    const x = xCss * dpr;
    let best = null, bd = Infinity;
    for (const p of this.data) {
      if (p[1] == null || !Number.isFinite(p[1])) continue;
      const d = Math.abs(px(p[0]) - x);
      if (d < bd) { bd = d; best = p; }
    }
    return best;
  }

  _onMove(e) {
    const rect = this.canvas.getBoundingClientRect();
    const p = this._nearest(e.clientX - rect.left);
    this.hover = p;
    this.draw();
    if (!p) { this._hideTip(); return; }
    const { px, py, dpr } = this._geom;
    const host = this.canvas.parentElement;
    if (!this.tip) {
      this.tip = document.createElement('div');
      this.tip.className = 'chart-tip';
      if (getComputedStyle(host).position === 'static') host.style.position = 'relative';
      host.append(this.tip);
    }
    const span = this.data[this.data.length - 1][0] - this.data[0][0];
    this.tip.replaceChildren();
    const v = document.createElement('div');
    v.textContent = `${fmt(p[1], this.opts.decimals)} ${this.opts.unit || ''}`.trim();
    const t = document.createElement('div');
    t.className = 't';
    t.textContent = span > 20 * 3600 ? fmtDateTime(p[0]) : fmtTime(p[0]);
    this.tip.append(v, t);
    const hostRect = host.getBoundingClientRect();
    const left = rect.left - hostRect.left + px(p[0]) / dpr;
    const top = rect.top - hostRect.top + py(p[1]) / dpr;
    this.tip.style.left = `${Math.min(Math.max(left, 50), hostRect.width - 50)}px`;
    this.tip.style.top = `${Math.max(top, 30)}px`;
  }

  _drawHover(ctx) {
    const p = this.hover;
    if (!p || !this._geom) return;
    const { px, py, dpr, m, ch } = this._geom;
    const x = px(p[0]), y = py(p[1]);
    ctx.strokeStyle = '#94a3b888';
    ctx.lineWidth = 1 * dpr;
    ctx.beginPath(); ctx.moveTo(x, m.t); ctx.lineTo(x, m.t + ch); ctx.stroke();
    ctx.beginPath(); ctx.arc(x, y, 4 * dpr, 0, Math.PI * 2);
    ctx.fillStyle = this.opts.color; ctx.fill();
    ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5 * dpr; ctx.stroke();
  }

  _hideTip() {
    if (this.tip) { this.tip.remove(); this.tip = null; }
  }
}
