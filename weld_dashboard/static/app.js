(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const STATES = {
    running:   { icon: "▶", label: "Running" },
    idle:      { icon: "❚❚", label: "Idle" },
    teach:     { icon: "✎", label: "Teach mode" },
    stopped:   { icon: "■", label: "Stopped" },
    alarm:     { icon: "⚠", label: "Alarm" },
    emergency: { icon: "⛔", label: "E-Stop" },
    offline:   { icon: "⦸", label: "No data" },
  };

  let cfg = { title: "TIG Weld Cell", target_parts_per_shift: 0, history_seconds: 600 };
  let history = [];
  let tolerance = 10;
  let lastMessage = 0;

  // ---- theme / fullscreen ------------------------------------------------
  const params = new URLSearchParams(location.search);
  function setTheme(t) {
    document.documentElement.dataset.theme = t;
    try { localStorage.setItem("weld-theme", t); } catch (e) { /* storage unavailable */ }
    charts.forEach((c) => c.draw());
  }
  let savedTheme = null;
  try { savedTheme = localStorage.getItem("weld-theme"); } catch (e) { /* ignore */ }
  document.documentElement.dataset.theme = params.get("theme") || savedTheme || "dark";
  $("theme-btn").onclick = () =>
    setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  $("fs-btn").onclick = () =>
    document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen();

  // ---- charts ---------------------------------------------------------------
  const currentChart = new LineChart($("chart-current"), {
    color: "--series-1", unit: "A", decimals: 0, minMax: 50, setKey: "set", tol: tolerance,
  });
  const voltageChart = new LineChart($("chart-voltage"), {
    color: "--series-2", unit: "V", decimals: 1, minMax: 5,
  });
  const charts = [currentChart, voltageChart];

  function redrawCharts() {
    currentChart.opts.tol = tolerance;
    currentChart.setData(history.map((p) => ({ t: p.t, y: p.current, set: p.current_set })));
    voltageChart.setData(history.map((p) => ({ t: p.t, y: p.voltage })));
  }

  // ---- formatting ----------------------------------------------------------
  const fmt = (v, d = 0) => (v == null || Number.isNaN(v) ? "—" : Number(v).toFixed(d));
  const pct = (v, d = 0) => (v == null ? "—" : (v * 100).toFixed(d));
  const pad = (x) => String(x).padStart(2, "0");
  const clock = (t, sec = true) => {
    const d = new Date(t * 1000);
    return pad(d.getHours()) + ":" + pad(d.getMinutes()) + (sec ? ":" + pad(d.getSeconds()) : "");
  };
  function dur(s) {
    if (s == null) return "—";
    s = Math.round(s);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    return h ? h + "h " + pad(m) + "m" : m ? m + "m " + pad(r) + "s" : r + "s";
  }
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

  // ---- render ----------------------------------------------------------------
  function render(d) {
    const live = d.live, shift = d.shift;
    tolerance = d.tolerance_pct;

    const st = STATES[live.robot_state] || { icon: "•", label: live.robot_state || "Unknown" };
    $("state").dataset.state = STATES[live.robot_state] ? live.robot_state : "idle";
    $("state-icon").textContent = st.icon;
    $("state-label").textContent = st.label;
    $("arc").dataset.on = String(!!live.arc_on);
    $("arc-label").textContent = live.arc_on ? "Arc on" : "Arc off";
    $("sub").textContent =
      "Program " + (live.program_no ?? "—") + " · Step " + (live.step_no ?? "—") +
      (live.cycle_count != null ? " · Counter " + live.cycle_count : "");

    const banner = $("banner");
    if (live.alarm_code) {
      banner.hidden = false;
      banner.className = "banner";
      banner.textContent = "⚠ Alarm " + live.alarm_code + (live.alarm_text ? " — " + live.alarm_text : "");
    } else if (!live.connected) {
      banner.hidden = false;
      banner.className = "banner warn";
      banner.textContent = "⦸ Not receiving data from the robot" + (d.source_error ? " — " + d.source_error : "");
    } else {
      banner.hidden = true;
    }

    const arc = live.arc_on;
    $("v-current").textContent = arc ? fmt(live.weld_current_a) : live.connected ? "0" : "—";
    const dev = live.current_dev_pct;
    let meta = "Set " + fmt(live.weld_current_set_a) + " A";
    if (dev != null) {
      const ok = Math.abs(dev) <= tolerance;
      meta += ' · <span class="' + (ok ? "ok" : "bad") + '">' + (ok ? "✓ " : "⚠ ") +
        (dev >= 0 ? "+" : "") + dev.toFixed(1) + "%</span>";
    }
    $("v-current-meta").innerHTML = meta;
    $("v-voltage").textContent = arc ? fmt(live.arc_voltage_v, 1) : live.connected ? "0.0" : "—";
    $("v-travel").textContent = arc ? fmt(live.travel_speed_cm_min, 1) : "—";
    $("v-heat").textContent = fmt(live.heat_input_kj_mm, 2);
    $("v-wire").textContent = arc ? fmt(live.wire_feed_m_min, 2) : "—";
    $("v-gas").textContent = fmt(live.gas_flow_l_min, 1);

    $("p-parts").textContent = shift.parts;
    const target = cfg.target_parts_per_shift;
    if (target > 0) {
      $("p-target").textContent = "/ " + target;
      $("p-progress").hidden = false;
      $("p-bar").style.width = Math.min(100, (shift.parts / target) * 100) + "%";
    }
    $("p-cycle").textContent = fmt(shift.avg_cycle_s, 1);
    $("p-cycle-meta").textContent = "Last " + (shift.last_cycle_s != null ? fmt(shift.last_cycle_s, 1) + " s" : "—");
    $("p-arc-ratio").textContent = pct(shift.arc_on_ratio);
    $("p-arc-meta").textContent = dur(shift.arc_on_seconds) + " arc time";
    $("p-util").textContent = pct(shift.utilization);
    $("p-util-meta").textContent = dur(shift.alarm_seconds) + " in alarm";
    $("p-quality").textContent = pct(shift.weld_quality_ratio, 1);
    $("p-quality-meta").textContent = shift.arc_starts + " welds · " + shift.welds_out_of_tolerance + " out of ±" + tolerance + "%";
    $("p-alarms").textContent = shift.alarm_count;
    $("shift-label").textContent = "Shift since " + clock(shift.start, false);

    const welds = d.recent_welds;
    $("welds").innerHTML = welds.length
      ? welds.map((w) =>
          "<tr><td>" + clock(w.start) + "</td><td>" + esc(w.program_no ?? "—") +
          '</td><td class="num">' + fmt(w.duration_s, 1) +
          '</td><td class="num">' + fmt(w.avg_current_a) +
          '</td><td class="num">' + fmt(w.avg_voltage_v, 1) +
          '</td><td class="num">' + (w.max_current_dev_pct != null ? fmt(w.max_current_dev_pct, 1) + "%" : "—") +
          '</td><td><span class="pill ' + (w.in_tolerance ? "ok" : "bad") + '">' +
          (w.in_tolerance ? "✓ OK" : "⚠ Out of tol.") + "</span></td></tr>").join("")
      : '<tr><td colspan="7" class="muted">No welds yet this session</td></tr>';

    $("alarms").innerHTML = d.alarms.length
      ? d.alarms.map((a) =>
          "<tr" + (a.cleared ? "" : ' class="active"') + "><td>" + clock(a.t) + "</td><td>" + esc(a.code) +
          '</td><td class="msg">' + esc(a.text) + '</td><td class="num">' +
          (a.cleared ? dur(a.cleared - a.t) : "active") + "</td></tr>").join("")
      : '<tr><td colspan="4" class="muted">No alarms</td></tr>';
  }

  function handle(d) {
    lastMessage = Date.now();
    if (d.history) history = d.history;
    else if (d.point) {
      history.push(d.point);
      const cutoff = d.point.t - cfg.history_seconds;
      while (history.length && history[0].t < cutoff) history.shift();
    }
    render(d);
    redrawCharts();
  }

  // ---- connection -----------------------------------------------------------
  function connect() {
    const es = new EventSource("api/stream");
    es.onmessage = (e) => handle(JSON.parse(e.data));
    es.onerror = () => {
      // EventSource reconnects on its own; a full reload of history happens on reconnect.
    };
  }

  setInterval(() => {
    $("clock").textContent = clock(Date.now() / 1000);
    if (lastMessage && Date.now() - lastMessage > 5000) {
      $("state").dataset.state = "offline";
      $("state-icon").textContent = STATES.offline.icon;
      $("state-label").textContent = "Dashboard offline";
    }
  }, 500);

  fetch("api/config")
    .then((r) => r.json())
    .then((c) => {
      cfg = Object.assign(cfg, c);
      document.title = cfg.title;
      $("title").textContent = cfg.title;
      currentChart.opts.windowSec = voltageChart.opts.windowSec = cfg.history_seconds;
      $("window-label").textContent = "Last " + Math.round(cfg.history_seconds / 60) + " min";
    })
    .catch(() => {})
    .finally(connect);
})();
