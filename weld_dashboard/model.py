"""Data model: the raw robot snapshot and the derived production metrics."""

from __future__ import annotations

import csv
import os
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Optional

ROBOT_STATES = ("running", "idle", "teach", "stopped", "alarm", "emergency", "offline")


@dataclass
class Snapshot:
    """One reading from the robot / welding power source.

    Every field except ``timestamp`` is optional: a data source fills in what
    its interface exposes and leaves the rest as ``None``.
    """

    timestamp: float = field(default_factory=time.time)
    connected: bool = True
    robot_state: Optional[str] = None  # one of ROBOT_STATES
    arc_on: Optional[bool] = None
    weld_current_a: Optional[float] = None
    weld_current_set_a: Optional[float] = None
    arc_voltage_v: Optional[float] = None
    travel_speed_cm_min: Optional[float] = None
    wire_feed_m_min: Optional[float] = None  # filler wire (hot/cold-wire TIG)
    gas_flow_l_min: Optional[float] = None
    program_no: Optional[int] = None
    step_no: Optional[int] = None
    cycle_count: Optional[int] = None  # controller part/cycle counter
    alarm_code: Optional[int] = None  # 0 / None = no alarm
    alarm_text: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def heat_input_kj_mm(self) -> Optional[float]:
        """Arc energy = V * I * 60 / (1000 * travel speed in mm/min)."""
        v, i, s = self.arc_voltage_v, self.weld_current_a, self.travel_speed_cm_min
        if not self.arc_on or v is None or i is None or not s:
            return None
        return v * i * 60.0 / (1000.0 * s * 10.0)


def _parse_hhmm(text: str) -> tuple[int, int]:
    h, m = text.split(":")
    return int(h), int(m)


def shift_start_for(now: datetime, shift_starts: list[str]) -> datetime:
    """Return the start of the shift that ``now`` falls in."""
    if not shift_starts:
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    candidates = []
    for day_offset in (0, -1):
        day = now + timedelta(days=day_offset)
        for s in shift_starts:
            h, m = _parse_hhmm(s)
            candidates.append(day.replace(hour=h, minute=m, second=0, microsecond=0))
    return max(c for c in candidates if c <= now)


@dataclass
class WeldRecord:
    """One arc-on segment (a single weld bead)."""

    start: float
    end: float
    program_no: Optional[int]
    avg_current_a: Optional[float]
    avg_voltage_v: Optional[float]
    avg_travel_cm_min: Optional[float]
    max_current_dev_pct: Optional[float]
    in_tolerance: bool

    @property
    def duration_s(self) -> float:
        return self.end - self.start


class _Mean:
    def __init__(self) -> None:
        self.n = 0
        self.total = 0.0

    def add(self, v: Optional[float]) -> None:
        if v is not None:
            self.n += 1
            self.total += v

    @property
    def value(self) -> Optional[float]:
        return self.total / self.n if self.n else None


class MetricsEngine:
    """Turns a stream of snapshots into shift-level production metrics."""

    def __init__(
        self,
        shift_starts: list[str],
        current_tolerance_pct: float = 10.0,
        tolerance_ignore_start_s: float = 1.0,
        history_seconds: int = 600,
        weld_log_dir: Optional[str] = None,
        max_alarms: int = 50,
    ) -> None:
        self.shift_starts = shift_starts
        self.current_tolerance_pct = current_tolerance_pct
        # Skip the arc-start upslope so the ramp isn't counted as a deviation.
        self.tolerance_ignore_start_s = tolerance_ignore_start_s
        self.history: deque[dict] = deque(maxlen=max(10, history_seconds * 2))
        self.history_seconds = history_seconds
        self.weld_log_dir = weld_log_dir
        self.alarms: deque[dict] = deque(maxlen=max_alarms)
        self.recent_welds: deque[WeldRecord] = deque(maxlen=20)
        self.latest: Optional[Snapshot] = None
        self._reset_shift(datetime.now())

    # ---- shift bookkeeping -------------------------------------------------

    def _reset_shift(self, now: datetime) -> None:
        self.shift_start = shift_start_for(now, self.shift_starts)
        self.arc_on_seconds = 0.0
        self.running_seconds = 0.0
        self.alarm_seconds = 0.0
        self.observed_seconds = 0.0
        self.arc_starts = 0
        self.parts = 0
        self.welds_out_of_tolerance = 0
        self.cycle_times: list[float] = []
        self.alarm_count = 0
        self._last_cycle_count: Optional[int] = None
        self._last_cycle_ts: Optional[float] = None
        self._weld: Optional[dict] = None

    def _maybe_roll_shift(self, ts: float) -> None:
        now = datetime.fromtimestamp(ts)
        if shift_start_for(now, self.shift_starts) != self.shift_start:
            self._reset_shift(now)

    # ---- ingestion ---------------------------------------------------------

    def ingest(self, snap: Snapshot) -> None:
        self._maybe_roll_shift(snap.timestamp)
        prev = self.latest
        dt = 0.0
        if prev is not None:
            dt = max(0.0, min(snap.timestamp - prev.timestamp, 5.0))  # ignore gaps

        if prev is not None and prev.connected:
            self.observed_seconds += dt
            if prev.arc_on:
                self.arc_on_seconds += dt
            if prev.robot_state == "running":
                self.running_seconds += dt
            if prev.robot_state in ("alarm", "emergency"):
                self.alarm_seconds += dt

        self._track_arc(prev, snap)
        self._track_cycles(snap)
        self._track_alarms(prev, snap)

        self.latest = snap
        point = {
            "t": snap.timestamp,
            "current": snap.weld_current_a if snap.arc_on else 0.0 if snap.connected else None,
            "current_set": snap.weld_current_set_a,
            "voltage": snap.arc_voltage_v if snap.arc_on else 0.0 if snap.connected else None,
            "travel": snap.travel_speed_cm_min if snap.arc_on else None,
        }
        self.history.append(point)
        cutoff = snap.timestamp - self.history_seconds
        while self.history and self.history[0]["t"] < cutoff:
            self.history.popleft()

    def _track_arc(self, prev: Optional[Snapshot], snap: Snapshot) -> None:
        was_on = bool(prev and prev.arc_on)
        if snap.arc_on and not was_on:
            self.arc_starts += 1
            self._weld = {
                "start": snap.timestamp,
                "program": snap.program_no,
                "i": _Mean(),
                "v": _Mean(),
                "s": _Mean(),
                "max_dev": None,
            }
        if snap.arc_on and self._weld is not None:
            w = self._weld
            w["i"].add(snap.weld_current_a)
            w["v"].add(snap.arc_voltage_v)
            w["s"].add(snap.travel_speed_cm_min)
            dev = self.current_deviation_pct(snap)
            if dev is not None and snap.timestamp - w["start"] >= self.tolerance_ignore_start_s:
                w["max_dev"] = abs(dev) if w["max_dev"] is None else max(w["max_dev"], abs(dev))
        if was_on and not snap.arc_on and self._weld is not None:
            self._close_weld(snap.timestamp)

    def _close_weld(self, end: float) -> None:
        w = self._weld
        self._weld = None
        max_dev = w["max_dev"]
        ok = max_dev is None or max_dev <= self.current_tolerance_pct
        rec = WeldRecord(
            start=w["start"],
            end=end,
            program_no=w["program"],
            avg_current_a=w["i"].value,
            avg_voltage_v=w["v"].value,
            avg_travel_cm_min=w["s"].value,
            max_current_dev_pct=max_dev,
            in_tolerance=ok,
        )
        if not ok:
            self.welds_out_of_tolerance += 1
        self.recent_welds.appendleft(rec)
        self._log_weld(rec)

    def _track_cycles(self, snap: Snapshot) -> None:
        if snap.cycle_count is None:
            return
        last = self._last_cycle_count
        if last is not None and snap.cycle_count > last:
            delta = snap.cycle_count - last
            if delta < 1000:  # guard against counter resets / garbage
                self.parts += delta
                if self._last_cycle_ts is not None and delta == 1:
                    self.cycle_times.append(snap.timestamp - self._last_cycle_ts)
                self._last_cycle_ts = snap.timestamp
        elif last is None:
            self._last_cycle_ts = None
        self._last_cycle_count = snap.cycle_count

    def _track_alarms(self, prev: Optional[Snapshot], snap: Snapshot) -> None:
        code = snap.alarm_code or 0
        prev_code = (prev.alarm_code or 0) if prev else 0
        if code and code != prev_code:
            self.alarm_count += 1
            self.alarms.appendleft(
                {"t": snap.timestamp, "code": code, "text": snap.alarm_text or "", "cleared": None}
            )
        if prev_code and code != prev_code:
            for a in self.alarms:
                if a["code"] == prev_code and a["cleared"] is None:
                    a["cleared"] = snap.timestamp
                    break

    def _log_weld(self, rec: WeldRecord) -> None:
        if not self.weld_log_dir:
            return
        os.makedirs(self.weld_log_dir, exist_ok=True)
        day = datetime.fromtimestamp(rec.start).strftime("%Y-%m-%d")
        path = os.path.join(self.weld_log_dir, f"welds-{day}.csv")
        new = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(
                    ["start", "end", "duration_s", "program", "avg_current_a", "avg_voltage_v",
                     "avg_travel_cm_min", "max_current_dev_pct", "in_tolerance"]
                )
            w.writerow(
                [datetime.fromtimestamp(rec.start).isoformat(timespec="seconds"),
                 datetime.fromtimestamp(rec.end).isoformat(timespec="seconds"),
                 f"{rec.duration_s:.1f}", rec.program_no, _fmt(rec.avg_current_a),
                 _fmt(rec.avg_voltage_v), _fmt(rec.avg_travel_cm_min),
                 _fmt(rec.max_current_dev_pct), int(rec.in_tolerance)]
            )

    # ---- output ------------------------------------------------------------

    def current_deviation_pct(self, snap: Snapshot) -> Optional[float]:
        if not snap.arc_on or snap.weld_current_a is None or not snap.weld_current_set_a:
            return None
        return (snap.weld_current_a - snap.weld_current_set_a) / snap.weld_current_set_a * 100.0

    def summary(self) -> dict:
        snap = self.latest or Snapshot(connected=False)
        state = snap.robot_state if snap.connected else "offline"
        cycle = self.cycle_times[-20:]
        welds_total = self.arc_starts
        return {
            "now": time.time(),
            "live": {
                **snap.to_dict(),
                "robot_state": state,
                "heat_input_kj_mm": snap.heat_input_kj_mm,
                "current_dev_pct": self.current_deviation_pct(snap)
                if self._weld is None
                or snap.timestamp - self._weld["start"] >= self.tolerance_ignore_start_s
                else None,
            },
            "shift": {
                "start": self.shift_start.timestamp(),
                "parts": self.parts,
                "arc_on_seconds": self.arc_on_seconds,
                "arc_on_ratio": (self.arc_on_seconds / self.observed_seconds)
                if self.observed_seconds else None,
                "utilization": (self.running_seconds / self.observed_seconds)
                if self.observed_seconds else None,
                "alarm_seconds": self.alarm_seconds,
                "arc_starts": self.arc_starts,
                "alarm_count": self.alarm_count,
                "avg_cycle_s": sum(cycle) / len(cycle) if cycle else None,
                "last_cycle_s": cycle[-1] if cycle else None,
                "welds_out_of_tolerance": self.welds_out_of_tolerance,
                "weld_quality_ratio": (1 - self.welds_out_of_tolerance / welds_total)
                if welds_total else None,
            },
            "alarms": list(self.alarms),
            "recent_welds": [
                {**asdict(r), "duration_s": r.duration_s} for r in self.recent_welds
            ],
            "tolerance_pct": self.current_tolerance_pct,
        }


def _fmt(v: Optional[float]) -> str:
    return "" if v is None else f"{v:.2f}"
