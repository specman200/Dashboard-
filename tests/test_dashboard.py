import os
import socketserver
import struct
import tempfile
import threading
import unittest
from datetime import datetime

from weld_dashboard.model import MetricsEngine, Snapshot, shift_start_for
from weld_dashboard.sources import create_source
from weld_dashboard.sources.modbus_tcp import decode


def snap(t, **kw):
    base = dict(timestamp=t, connected=True, robot_state="running", arc_on=False)
    base.update(kw)
    return Snapshot(**base)


class ShiftTests(unittest.TestCase):
    def test_shift_boundaries(self):
        starts = ["06:00", "14:00", "22:00"]
        self.assertEqual(shift_start_for(datetime(2026, 1, 2, 7, 0), starts), datetime(2026, 1, 2, 6, 0))
        self.assertEqual(shift_start_for(datetime(2026, 1, 2, 23, 0), starts), datetime(2026, 1, 2, 22, 0))
        # Night shift spans midnight
        self.assertEqual(shift_start_for(datetime(2026, 1, 2, 3, 0), starts), datetime(2026, 1, 1, 22, 0))


class MetricsTests(unittest.TestCase):
    def make(self, **kw):
        return MetricsEngine(shift_starts=[], current_tolerance_pct=10, tolerance_ignore_start_s=0, **kw)

    def test_arc_time_parts_and_weld_records(self):
        e = self.make()
        t = datetime.now().replace(hour=12).timestamp()
        e.ingest(snap(t, cycle_count=5))
        for i in range(1, 11):  # 10 s of welding at 1 Hz, on setpoint
            e.ingest(snap(t + i, arc_on=True, weld_current_a=150, weld_current_set_a=150,
                          arc_voltage_v=11, travel_speed_cm_min=20, cycle_count=5))
        e.ingest(snap(t + 11, cycle_count=6))
        e.ingest(snap(t + 20, cycle_count=6, robot_state="idle"))
        e.ingest(snap(t + 40, cycle_count=7))
        s = e.summary()["shift"]
        self.assertEqual(s["arc_starts"], 1)
        self.assertAlmostEqual(s["arc_on_seconds"], 10.0)
        self.assertEqual(s["parts"], 2)
        self.assertEqual(e.recent_welds[0].in_tolerance, True)
        self.assertAlmostEqual(e.recent_welds[0].avg_current_a, 150)
        self.assertEqual(s["weld_quality_ratio"], 1.0)

    def test_out_of_tolerance_weld(self):
        e = self.make()
        t = 1_000_000.0
        e.ingest(snap(t))
        e.ingest(snap(t + 1, arc_on=True, weld_current_a=150, weld_current_set_a=150))
        e.ingest(snap(t + 2, arc_on=True, weld_current_a=120, weld_current_set_a=150))  # -20 %
        e.ingest(snap(t + 3))
        self.assertFalse(e.recent_welds[0].in_tolerance)
        self.assertAlmostEqual(e.recent_welds[0].max_current_dev_pct, 20.0)
        self.assertEqual(e.summary()["shift"]["welds_out_of_tolerance"], 1)

    def test_alarm_log_and_clear(self):
        e = self.make()
        t = 1_000_000.0
        e.ingest(snap(t))
        e.ingest(snap(t + 1, robot_state="alarm", alarm_code=1305, alarm_text="Gas flow low"))
        e.ingest(snap(t + 2, robot_state="alarm", alarm_code=1305, alarm_text="Gas flow low"))
        e.ingest(snap(t + 9, alarm_code=0))
        s = e.summary()
        self.assertEqual(s["shift"]["alarm_count"], 1)
        self.assertEqual(s["alarms"][0]["code"], 1305)
        self.assertEqual(s["alarms"][0]["cleared"], t + 9)

    def test_heat_input(self):
        s = Snapshot(arc_on=True, weld_current_a=150, arc_voltage_v=12, travel_speed_cm_min=15)
        # 12 V * 150 A * 60 / (1000 * 150 mm/min) = 0.72 kJ/mm
        self.assertAlmostEqual(s.heat_input_kj_mm, 0.72)

    def test_weld_log_written(self):
        with tempfile.TemporaryDirectory() as d:
            e = self.make(weld_log_dir=d)
            t = 1_000_000.0
            e.ingest(snap(t))
            e.ingest(snap(t + 1, arc_on=True, weld_current_a=100, weld_current_set_a=100))
            e.ingest(snap(t + 2))
            files = os.listdir(d)
            self.assertEqual(len(files), 1)
            with open(os.path.join(d, files[0])) as f:
                self.assertEqual(len(f.read().strip().splitlines()), 2)


class FakeModbus(socketserver.BaseRequestHandler):
    registers = {}

    def handle(self):
        while True:
            hdr = self.request.recv(7)
            if len(hdr) < 7:
                return
            tid, _, length, unit = struct.unpack(">HHHB", hdr)
            func, addr, count = struct.unpack(">BHH", self.request.recv(length - 1))
            words = [self.registers.get(addr + i, 0) for i in range(count)]
            pdu = struct.pack(">BB", func, count * 2) + struct.pack(f">{count}H", *words)
            self.request.sendall(struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu)


class ModbusTests(unittest.TestCase):
    def test_decode(self):
        self.assertEqual(decode([0xFFFF], "int16"), -1)
        hi, lo = struct.unpack(">HH", struct.pack(">f", 11.5))
        self.assertAlmostEqual(decode([hi, lo], "float32"), 11.5)
        self.assertAlmostEqual(decode([lo, hi], "float32", "little"), 11.5)

    def test_read_from_fake_device(self):
        hi, lo = struct.unpack(">HH", struct.pack(">f", 11.5))
        FakeModbus.registers = {100: 2, 101: 0b10, 102: 1534, 103: 1500, 104: hi, 105: lo,
                                200: 0, 201: 42}
        srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), FakeModbus)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            src = create_source({
                "type": "modbus_tcp", "host": "127.0.0.1", "port": srv.server_address[1],
                "registers": {
                    "robot_state": {"address": 100},
                    "arc_on": {"address": 101, "bit": 1},
                    "weld_current_a": {"address": 102, "scale": 0.1},
                    "weld_current_set_a": {"address": 103, "scale": 0.1},
                    "arc_voltage_v": {"address": 104, "type": "float32"},
                    "cycle_count": {"address": 200, "type": "uint32"},
                },
            })
            src.connect()
            s = src.read()
            src.close()
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(s.robot_state, "running")
        self.assertTrue(s.arc_on)
        self.assertAlmostEqual(s.weld_current_a, 153.4)
        self.assertAlmostEqual(s.arc_voltage_v, 11.5)
        self.assertEqual(s.cycle_count, 42)


class CsvTests(unittest.TestCase):
    def test_tail_reads_only_new_rows(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "log.csv")
            with open(path, "w") as f:
                f.write("Time,Current,Voltage,State\n1,999,99,2\n")
            src = create_source({"type": "csv", "path": path, "columns": {
                "weld_current_a": "Current", "arc_voltage_v": "Voltage", "robot_state": "State"}})
            src.connect()
            self.assertIsNone(src.read().weld_current_a)  # old row not replayed
            with open(path, "a") as f:
                f.write("2,150.5,11.2,2\n")
            s = src.read()
            self.assertAlmostEqual(s.weld_current_a, 150.5)
            self.assertTrue(s.arc_on)
            self.assertEqual(s.robot_state, "running")


class SimulatorTests(unittest.TestCase):
    def test_produces_readings(self):
        src = create_source({"type": "simulator", "seed": 1})
        s = src.read()
        self.assertTrue(s.connected)
        self.assertIn(s.robot_state, ("idle", "running", "alarm"))

    def test_runs_through_many_cycles(self):
        src = create_source({"type": "simulator", "seed": 2, "alarm_probability": 0.5})
        clock = [src._t0]
        src._sim_time = lambda: clock[0]
        e = MetricsEngine(shift_starts=[])
        states = set()
        for i in range(4000):  # ~2000 simulated seconds at 2 Hz
            clock[0] += 0.5
            s = src.read()
            s.timestamp = clock[0]
            states.add(s.robot_state)
            e.ingest(s)
        self.assertEqual(states, {"idle", "running", "alarm"})
        self.assertGreater(e.parts, 10)
        self.assertGreater(e.alarm_count, 0)


if __name__ == "__main__":
    unittest.main()
