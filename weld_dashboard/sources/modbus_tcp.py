"""Modbus TCP source (standard library only).

OTC Daihen FD-series controllers usually hand process data to the plant
through a fieldbus/PLC or a Modbus TCP gateway. Map each dashboard field to a
holding/input register in the config file:

    [source.registers.weld_current_a]
    address = 100        # 0-based register address
    type = "uint16"      # uint16 | int16 | uint32 | int32 | float32 | bool
    scale = 0.1          # raw * scale
    table = "holding"    # holding | input

The addresses depend entirely on how your controller / PLC is configured, so
none are assumed here.
"""

from __future__ import annotations

import socket
import struct
import time

from ..model import Snapshot
from .base import DataSource

_FUNC = {"holding": 3, "input": 4}
_WORDS = {"uint16": 1, "int16": 1, "bool": 1, "uint32": 2, "int32": 2, "float32": 2}
_NUMERIC_FIELDS = {
    "weld_current_a", "weld_current_set_a", "arc_voltage_v", "travel_speed_cm_min",
    "wire_feed_m_min", "gas_flow_l_min",
}
_INT_FIELDS = {"program_no", "step_no", "cycle_count", "alarm_code"}


class ModbusError(Exception):
    pass


class ModbusTcpClient:
    """Minimal Modbus TCP client supporting function codes 3 and 4."""

    def __init__(self, host: str, port: int = 502, unit_id: int = 1, timeout: float = 2.0):
        self.host, self.port, self.unit_id, self.timeout = host, port, unit_id, timeout
        self._sock: socket.socket | None = None
        self._tid = 0

    def connect(self) -> None:
        self.close()
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ModbusError("connection closed by device")
            buf += chunk
        return buf

    def read_registers(self, address: int, count: int, table: str = "holding") -> list[int]:
        if self._sock is None:
            self.connect()
        func = _FUNC[table]
        self._tid = (self._tid + 1) & 0xFFFF
        pdu = struct.pack(">BHH", func, address, count)
        mbap = struct.pack(">HHHB", self._tid, 0, len(pdu) + 1, self.unit_id)
        self._sock.sendall(mbap + pdu)
        tid, _proto, length, _unit = struct.unpack(">HHHB", self._recv_exact(7))
        body = self._recv_exact(length - 1)
        if tid != self._tid:
            raise ModbusError(f"transaction id mismatch ({tid} != {self._tid})")
        if body[0] & 0x80:
            raise ModbusError(f"device exception code {body[1]}")
        nbytes = body[1]
        return list(struct.unpack(f">{nbytes // 2}H", body[2 : 2 + nbytes]))


def decode(words: list[int], kind: str, word_order: str = "big") -> float | int | bool:
    if kind == "uint16":
        return words[0]
    if kind == "int16":
        return struct.unpack(">h", struct.pack(">H", words[0]))[0]
    if kind == "bool":
        return bool(words[0])
    hi, lo = (words[0], words[1]) if word_order == "big" else (words[1], words[0])
    raw = struct.pack(">HH", hi, lo)
    return struct.unpack({"uint32": ">I", "int32": ">i", "float32": ">f"}[kind], raw)[0]


class ModbusTcpSource(DataSource):
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.client = ModbusTcpClient(
            config["host"],
            int(config.get("port", 502)),
            int(config.get("unit_id", 1)),
            float(config.get("timeout", 2.0)),
        )
        self.word_order = config.get("word_order", "big")
        self.registers: dict = config.get("registers", {})
        self.alarm_texts = {int(k): v for k, v in config.get("alarm_texts", {}).items()}
        self._blocks = self._plan_blocks()

    def _plan_blocks(self) -> list[tuple[str, int, int]]:
        """Group registers into contiguous reads (max 120 words) to minimise round trips."""
        spans: dict[str, list[tuple[int, int]]] = {}
        for spec in self.registers.values():
            table = spec.get("table", "holding")
            start = int(spec["address"])
            spans.setdefault(table, []).append((start, start + _WORDS[spec.get("type", "uint16")]))
        blocks = []
        for table, items in spans.items():
            items.sort()
            cur_s, cur_e = items[0]
            for s, e in items[1:]:
                if s <= cur_e + 8 and e - cur_s <= 120:
                    cur_e = max(cur_e, e)
                else:
                    blocks.append((table, cur_s, cur_e - cur_s))
                    cur_s, cur_e = s, e
            blocks.append((table, cur_s, cur_e - cur_s))
        return blocks

    def connect(self) -> None:
        self.client.connect()

    def close(self) -> None:
        self.client.close()

    def read(self) -> Snapshot:
        mem: dict[tuple[str, int], int] = {}
        for table, start, count in self._blocks:
            for i, w in enumerate(self.client.read_registers(start, count, table)):
                mem[(table, start + i)] = w

        values: dict = {}
        for name, spec in self.registers.items():
            table = spec.get("table", "holding")
            kind = spec.get("type", "uint16")
            addr = int(spec["address"])
            words = [mem[(table, addr + i)] for i in range(_WORDS[kind])]
            raw = decode(words, kind, spec.get("word_order", self.word_order))
            if "bit" in spec:
                raw = bool((int(raw) >> int(spec["bit"])) & 1)
            elif kind != "bool":
                raw = raw * float(spec.get("scale", 1.0)) + float(spec.get("offset", 0.0))
            values[name] = raw

        snap = Snapshot(timestamp=time.time(), connected=True)
        for name, raw in values.items():
            if name in _NUMERIC_FIELDS:
                setattr(snap, name, float(raw))
            elif name in _INT_FIELDS:
                setattr(snap, name, int(round(raw)))
            elif name == "arc_on":
                snap.arc_on = bool(raw)
            elif name == "robot_state":
                snap.robot_state = self.decode_state(int(round(raw)))
        if snap.alarm_code:
            snap.alarm_text = self.alarm_texts.get(snap.alarm_code, f"Alarm {snap.alarm_code}")
            if snap.robot_state is None:
                snap.robot_state = "alarm"
        if snap.arc_on is None and snap.weld_current_a is not None:
            snap.arc_on = snap.weld_current_a > float(self.config.get("arc_on_threshold_a", 5.0))
        return snap
