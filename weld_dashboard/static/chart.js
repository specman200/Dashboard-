// Minimal canvas time-series chart: one series, optional setpoint line + tolerance band,
// crosshair tooltip. No external libraries so the dashboard runs on an offline network.
(function () {
  "use strict";

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function niceStep(range, target) {
    const raw = range / target;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const n = raw / mag;
    return (n >= 5 ? 10 : n >= 2 ? 5 : n >= 1 ? 2 : 1) * mag;
  }

  function hhmm(t, sec) {
    const d = new Date(t * 1000);
    const p = (x) => String(x).padStart(2, "0");
    return p(d.getHours()) + ":" + p(d.getMinutes()) + (sec ? ":" + p(d.getSeconds()) : "");
  }

  class LineChart {
    constructor(el, opts) {
      this.el = el;
      this.opts = Object.assign(
        { color: "--series-1", unit: "", decimals: 0, minMax: 10, windowSec: 600, setKey: null },
        opts
      );
      this.data = [];
      this.hover = null;
      this.canvas = document.createElement("canvas");
      el.appendChild(this.canvas);
      this.ctx = this.canvas.getContext("2d");
      this.tooltip = document.getElementById("tooltip");
      new ResizeObserver(() => this.draw()).observe(el);
      this.canvas.addEventListener("pointermove", (e) => this.onMove(e));
      this.canvas.addEventListener("pointerleave", () => {
        this.hover = null;
        this.tooltip.hidden = true;
        this.draw();
      });
    }

    setData(points) {
      this.data = points;
      this.draw();
    }

    geometry() {
      const w = this.el.clientWidth, h = this.el.clientHeight;
      const pad = { l: 44, r: 12, t: 8, b: 24 };
      const now = this.data.length ? this.data[this.data.length - 1].t : Date.now() / 1000;
      const t0 = now - this.opts.windowSec;
      let max = this.opts.minMax;
      for (const p of this.data) {
        if (p.y != null && p.y > max) max = p.y;
        if (p.set != null && p.set * (1 + (this.opts.tol || 0) / 100) > max)
          max = p.set * (1 + (this.opts.tol || 0) / 100);
      }
      const step = niceStep(max, 4);
      max = Math.ceil((max * 1.05) / step) * step;
      const x = (t) => pad.l + ((t - t0) / this.opts.windowSec) * (w - pad.l - pad.r);
      const y = (v) => pad.t + (1 - v / max) * (h - pad.t - pad.b);
      return { w, h, pad, t0, now, max, step, x, y };
    }

    draw() {
      const dpr = window.devicePixelRatio || 1;
      const w = this.el.clientWidth, h = this.el.clientHeight;
      if (!w || !h) return;
      if (this.canvas.width !== Math.round(w * dpr) || this.canvas.height !== Math.round(h * dpr)) {
        this.canvas.width = Math.round(w * dpr);
        this.canvas.height = Math.round(h * dpr);
      }
      const c = this.ctx;
      c.setTransform(dpr, 0, 0, dpr, 0, 0);
      c.clearRect(0, 0, w, h);
      const g = this.geometry();
      const font = getComputedStyle(document.body).fontFamily;
      c.font = "12px " + font;

      // Grid + y labels
      c.strokeStyle = css("--grid");
      c.fillStyle = css("--muted");
      c.lineWidth = 1;
      c.textAlign = "right";
      c.textBaseline = "middle";
      for (let v = 0; v <= g.max + 1e-9; v += g.step) {
        const yy = Math.round(g.y(v)) + 0.5;
        c.beginPath();
        c.moveTo(g.pad.l, yy);
        c.lineTo(w - g.pad.r, yy);
        c.stroke();
        c.fillText(v.toFixed(g.step < 1 ? 1 : 0), g.pad.l - 6, yy);
      }
      // Baseline
      c.strokeStyle = css("--axis");
      c.beginPath();
      c.moveTo(g.pad.l, Math.round(g.y(0)) + 0.5);
      c.lineTo(w - g.pad.r, Math.round(g.y(0)) + 0.5);
      c.stroke();

      // X labels every 2 minutes (every minute for short windows)
      const xStep = this.opts.windowSec > 300 ? 120 : 60;
      c.textAlign = "center";
      c.textBaseline = "top";
      for (let t = Math.ceil(g.t0 / xStep) * xStep; t <= g.now; t += xStep) {
        c.fillText(hhmm(t), g.x(t), h - g.pad.b + 6);
      }

      const pts = this.data.filter((p) => p.t >= g.t0);

      // Tolerance band + setpoint, drawn only while welding (where set & y exist)
      if (this.opts.setKey) {
        const tol = (this.opts.tol || 0) / 100;
        c.fillStyle = css("--band");
        this.segments(pts, (p) => p.set != null && p.y > 0).forEach((seg) => {
          c.beginPath();
          seg.forEach((p, i) => (i ? c.lineTo : c.moveTo).call(c, g.x(p.t), g.y(p.set * (1 + tol))));
          for (let i = seg.length - 1; i >= 0; i--) c.lineTo(g.x(seg[i].t), g.y(seg[i].set * (1 - tol)));
          c.closePath();
          c.fill();
        });
        c.strokeStyle = css("--muted");
        c.setLineDash([5, 4]);
        c.lineWidth = 1.5;
        this.segments(pts, (p) => p.set != null && p.y > 0).forEach((seg) => {
          c.beginPath();
          seg.forEach((p, i) => (i ? c.lineTo : c.moveTo).call(c, g.x(p.t), g.y(p.set)));
          c.stroke();
        });
        c.setLineDash([]);
      }

      // Series line
      c.strokeStyle = css(this.opts.color);
      c.lineWidth = 2;
      c.lineJoin = "round";
      this.segments(pts, (p) => p.y != null).forEach((seg) => {
        c.beginPath();
        seg.forEach((p, i) => (i ? c.lineTo : c.moveTo).call(c, g.x(p.t), g.y(p.y)));
        c.stroke();
      });

      // Crosshair
      if (this.hover) {
        const p = this.hover;
        const xx = Math.round(g.x(p.t)) + 0.5;
        c.strokeStyle = css("--ink-2");
        c.lineWidth = 1;
        c.beginPath();
        c.moveTo(xx, g.pad.t);
        c.lineTo(xx, g.y(0));
        c.stroke();
        if (p.y != null) {
          c.fillStyle = css(this.opts.color);
          c.strokeStyle = css("--surface");
          c.lineWidth = 2;
          c.beginPath();
          c.arc(g.x(p.t), g.y(p.y), 4.5, 0, Math.PI * 2);
          c.fill();
          c.stroke();
        }
      }
    }

    segments(pts, ok) {
      const out = [];
      let cur = [];
      for (const p of pts) {
        if (ok(p)) cur.push(p);
        else if (cur.length) { out.push(cur); cur = []; }
      }
      if (cur.length) out.push(cur);
      return out;
    }

    onMove(e) {
      if (!this.data.length) return;
      const g = this.geometry();
      const rect = this.canvas.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const t = g.t0 + ((mx - g.pad.l) / (g.w - g.pad.l - g.pad.r)) * this.opts.windowSec;
      let best = null, bestD = Infinity;
      for (const p of this.data) {
        const d = Math.abs(p.t - t);
        if (d < bestD) { bestD = d; best = p; }
      }
      this.hover = best;
      this.draw();
      const tip = this.tooltip;
      const val = best.y == null ? "no data" : best.y.toFixed(this.opts.decimals) + " " + this.opts.unit;
      let html = '<div class="t">' + hhmm(best.t, true) + "</div><b>" + val + "</b>";
      if (this.opts.setKey && best.set != null && best.y > 0)
        html += '<div class="t">Setpoint ' + best.set.toFixed(0) + " " + this.opts.unit + "</div>";
      tip.innerHTML = html;
      tip.hidden = false;
      const tw = tip.offsetWidth;
      const left = e.clientX + 14 + tw > window.innerWidth ? e.clientX - 14 - tw : e.clientX + 14;
      tip.style.left = left + "px";
      tip.style.top = e.clientY - 10 + "px";
    }
  }

  window.LineChart = LineChart;
})();
