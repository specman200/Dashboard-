"""Follow a CSV file that another program appends to.

Use this when the welding data is already being logged to disk, e.g. by the
power source's monitoring software, a PLC data logger, or a script that reads
the controller. Each new row becomes one snapshot. Map CSV columns to fields:

    [source.columns]
    weld_current_a = "Current[A]"
    arc_voltage_v  = "Voltage[V]"
"""

from __future__ import annotations

import csv
import io
import os
import time

from ..model import Snapshot
from .base import DataSource

_FLOAT = {"weld_current_a", "weld_current_set_a", "arc_voltage_v", "travel_speed_cm_min",
          "wire_feed_m_min", "gas_flow_l_min"}
_INT = {"program_no", "step_no", "cycle_count", "alarm_code"}


class CsvTailSource(DataSource):
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.path = config["path"]
        self.columns: dict[str, str] = config.get("columns", {})
        self.delimiter = config.get("delimiter", ",")
        self.stale_after = float(config.get("stale_after_s", 10))
        self._pos = 0
        self._header: list[str] | None = None
        self._last: Snapshot | None = None
        self._last_new_row = 0.0

    def connect(self) -> None:
        if not os.path.exists(self.path):
            raise FileNotFoundError(self.path)
        with open(self.path, newline="") as f:
            self._header = next(csv.reader(f, delimiter=self.delimiter), None)
            # Start at the end so old rows are not replayed as live data.
            f.seek(0, os.SEEK_END)
            self._pos = f.tell()
        self._last_new_row = time.time()

    def read(self) -> Snapshot:
        if self._header is None:
            self.connect()
        if os.path.getsize(self.path) < self._pos:  # file rotated / truncated
            self.connect()
        with open(self.path, newline="") as f:
            f.seek(self._pos)
            chunk = f.read()
            # Only consume complete lines.
            cut = chunk.rfind("\n") + 1
            self._pos += len(chunk[:cut].encode()) if cut else 0
            text = chunk[:cut]
        rows = list(csv.reader(io.StringIO(text), delimiter=self.delimiter))
        if rows:
            self._last = self._row_to_snapshot(rows[-1])
            self._last_new_row = time.time()
        if self._last is None:
            return Snapshot(connected=True, robot_state="idle")
        snap = Snapshot(**{**self._last.to_dict(), "timestamp": time.time()})
        if time.time() - self._last_new_row > self.stale_after:
            snap.connected = False
        return snap

    def _row_to_snapshot(self, row: list[str]) -> Snapshot:
        rec = dict(zip(self._header, row))
        snap = Snapshot(timestamp=time.time(), connected=True)
        for field, col in self.columns.items():
            raw = (rec.get(col) or "").strip()
            if raw == "":
                continue
            if field in _FLOAT:
                setattr(snap, field, float(raw))
            elif field in _INT:
                setattr(snap, field, int(float(raw)))
            elif field == "arc_on":
                snap.arc_on = raw.lower() in ("1", "true", "on", "yes")
            elif field == "robot_state":
                snap.robot_state = self.decode_state(raw)
            elif field == "alarm_text":
                snap.alarm_text = raw
        if snap.arc_on is None and snap.weld_current_a is not None:
            snap.arc_on = snap.weld_current_a > float(self.config.get("arc_on_threshold_a", 5.0))
        return snap
