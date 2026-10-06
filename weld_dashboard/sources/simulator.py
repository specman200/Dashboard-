"""Simulated TIG welding cell, so the dashboard can be run and demoed without a robot."""

from __future__ import annotations

import math
import random
import time

from ..model import Snapshot
from .base import DataSource

# (current setpoint A, voltage V, travel cm/min, wire m/min, duration s)
_BEADS = [
    (160, 11.5, 18, 1.2, 9),
    (145, 11.0, 16, 1.0, 6),
    (175, 12.2, 20, 1.4, 11),
]

_ALARMS = [
    (1201, "Arc start failure"),
    (1305, "Gas flow low"),
    (2010, "Torch collision detected"),
]


class SimulatorSource(DataSource):
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.speed = float(config.get("speed", 1.0))
        self.alarm_probability = float(config.get("alarm_probability", 0.08))
        self.rng = random.Random(config.get("seed"))
        self._t0 = time.time()
        self._cycle_count = int(config.get("start_count", 1200))
        self._phase_start = self._t0
        self._phase = "load"
        self._bead = 0
        self._alarm = None

    def _sim_time(self) -> float:
        return self._t0 + (time.time() - self._t0) * self.speed

    def _next_phase(self, now: float) -> None:
        self._phase_start = now
        if self._phase == "load":
            self._phase, self._bead = "move", 0
        elif self._phase == "move":
            self._phase = "weld"
        elif self._phase == "weld":
            self._bead += 1
            if self._bead >= len(_BEADS):
                self._cycle_count += 1
                if self.rng.random() < self.alarm_probability:
                    self._phase, self._alarm = "alarm", self.rng.choice(_ALARMS)
                else:
                    self._phase = "load"
            else:
                self._phase = "move"
        elif self._phase == "alarm":
            self._phase, self._alarm = "load", None

    def _phase_length(self) -> float:
        if self._phase == "weld":
            return _BEADS[self._bead][4]
        return {"load": 12.0, "move": 3.0, "alarm": 25.0}[self._phase]

    def read(self) -> Snapshot:
        now = self._sim_time()
        while now - self._phase_start >= self._phase_length():
            self._next_phase(self._phase_start + self._phase_length())

        set_a, volt, travel, wire, _ = _BEADS[min(self._bead, len(_BEADS) - 1)]
        arc = self._phase == "weld"
        elapsed = now - self._phase_start
        ramp = min(1.0, elapsed / 0.8)  # upslope
        wobble = math.sin(now * 2.3) * 0.02 + self.rng.gauss(0, 0.012)
        # Occasional current excursion to exercise the tolerance check.
        excursion = 0.14 if arc and self._bead == 2 and int(self._cycle_count) % 7 == 0 else 0.0
        state = {"load": "idle", "move": "running", "weld": "running", "alarm": "alarm"}[
            self._phase
        ]
        return Snapshot(
            timestamp=time.time(),
            connected=True,
            robot_state=state,
            arc_on=arc,
            weld_current_a=set_a * ramp * (1 + wobble + excursion) if arc else 0.0,
            weld_current_set_a=float(set_a),
            arc_voltage_v=volt * (1 + wobble * 0.6) if arc else 0.0,
            travel_speed_cm_min=travel * (1 + self.rng.gauss(0, 0.01)) if arc else 0.0,
            wire_feed_m_min=wire if arc else 0.0,
            gas_flow_l_min=(12.0 + self.rng.gauss(0, 0.15)) if self._phase != "load" else 0.0,
            program_no=7,
            step_no=10 + self._bead * 2 + (1 if arc else 0),
            cycle_count=self._cycle_count,
            alarm_code=self._alarm[0] if self._alarm else 0,
            alarm_text=self._alarm[1] if self._alarm else None,
        )
