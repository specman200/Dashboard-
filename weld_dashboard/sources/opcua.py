"""OPC UA source (needs ``pip install asyncua``).

Useful when the cell has an OPC UA server in front of it (a PLC, an IIoT
gateway such as Kepware, or the robot vendor's own data-collection software).
Map dashboard fields to node ids in the config:

    [source.nodes]
    weld_current_a = "ns=2;s=Robot1.Weld.Current"
    arc_on         = "ns=2;s=Robot1.Weld.ArcOn"
"""

from __future__ import annotations

import time

from ..model import Snapshot
from .base import DataSource

_INT_FIELDS = {"program_no", "step_no", "cycle_count", "alarm_code"}


class OpcUaSource(DataSource):
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        try:
            from asyncua.sync import Client  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on optional package
            raise RuntimeError(
                "OPC UA source needs the 'asyncua' package: pip install asyncua"
            ) from exc
        self._client_cls = Client
        self.url = config["url"]
        self.nodes_cfg: dict[str, str] = config.get("nodes", {})
        self.scales: dict[str, float] = config.get("scales", {})
        self.client = None
        self._nodes = {}

    def connect(self) -> None:
        self.close()
        self.client = self._client_cls(self.url, timeout=float(self.config.get("timeout", 4)))
        if self.config.get("username"):
            self.client.set_user(self.config["username"])
            self.client.set_password(self.config.get("password", ""))
        self.client.connect()
        self._nodes = {k: self.client.get_node(v) for k, v in self.nodes_cfg.items()}

    def close(self) -> None:
        if self.client is not None:
            try:
                self.client.disconnect()
            except Exception:
                pass
            self.client = None

    def read(self) -> Snapshot:
        if self.client is None:
            self.connect()
        names = list(self._nodes)
        values = self.client.read_values([self._nodes[n] for n in names])
        snap = Snapshot(timestamp=time.time(), connected=True)
        for name, val in zip(names, values):
            if name == "robot_state":
                snap.robot_state = self.decode_state(val)
            elif name == "arc_on":
                snap.arc_on = bool(val)
            elif name == "alarm_text":
                snap.alarm_text = str(val) if val else None
            elif hasattr(snap, name) and val is not None:
                v = float(val) * float(self.scales.get(name, 1.0))
                setattr(snap, name, int(v) if name in _INT_FIELDS else v)
        if snap.arc_on is None and snap.weld_current_a is not None:
            snap.arc_on = snap.weld_current_a > float(self.config.get("arc_on_threshold_a", 5.0))
        return snap
