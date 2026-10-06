"""Run the dashboard:  python -m weld_dashboard [--config config.toml]"""

from __future__ import annotations

import argparse
import logging
import sys
import tomllib
from pathlib import Path

from .model import MetricsEngine
from .server import Poller, serve
from .sources import create_source

DEFAULTS = {
    "server": {"host": "0.0.0.0", "port": 8080},
    "dashboard": {
        "title": "TIG Weld Cell",
        "poll_interval_s": 0.5,
        "history_seconds": 600,
        "shift_starts": ["06:00", "14:00", "22:00"],
        "current_tolerance_pct": 10.0,
        "tolerance_ignore_start_s": 1.0,
        "target_parts_per_shift": 0,
        "weld_log_dir": "weld_logs",
    },
    "source": {"type": "simulator"},
}


def load_config(path: str | None) -> dict:
    cfg = {k: dict(v) for k, v in DEFAULTS.items()}
    if path:
        with open(path, "rb") as f:
            user = tomllib.load(f)
        for section, values in user.items():
            if isinstance(values, dict):
                cfg.setdefault(section, {}).update(values)
            else:
                cfg[section] = values
    return cfg


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="OTC Daihen TIG robot dashboard")
    ap.add_argument("--config", "-c", help="TOML config file (default: config.toml if present)")
    ap.add_argument("--simulate", action="store_true", help="force the built-in simulator")
    ap.add_argument("--port", type=int, help="override HTTP port")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    path = args.config or ("config.toml" if Path("config.toml").exists() else None)
    cfg = load_config(path)
    if args.simulate:
        cfg["source"] = {"type": "simulator"}
    if args.port:
        cfg["server"]["port"] = args.port

    dash = cfg["dashboard"]
    engine = MetricsEngine(
        shift_starts=dash["shift_starts"],
        current_tolerance_pct=float(dash["current_tolerance_pct"]),
        tolerance_ignore_start_s=float(dash["tolerance_ignore_start_s"]),
        history_seconds=int(dash["history_seconds"]),
        weld_log_dir=dash.get("weld_log_dir") or None,
    )
    source = create_source(cfg["source"])
    poller = Poller(source, engine, float(dash["poll_interval_s"]))
    poller.start()

    ui = {
        "title": dash["title"],
        "target_parts_per_shift": dash.get("target_parts_per_shift", 0),
        "source_type": cfg["source"].get("type"),
        "history_seconds": dash["history_seconds"],
    }
    host, port = cfg["server"]["host"], int(cfg["server"]["port"])
    httpd = serve(poller, host, port, ui)
    logging.info("source=%s  config=%s", cfg["source"].get("type"), path or "(defaults)")
    logging.info("dashboard on http://%s:%d/", "localhost" if host == "0.0.0.0" else host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        poller.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
