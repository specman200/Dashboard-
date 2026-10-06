"""Robot data sources. Pick one with ``[source] type = "..."`` in the config."""

from __future__ import annotations

from .base import DataSource


def create_source(config: dict) -> DataSource:
    kind = config.get("type", "simulator")
    if kind == "simulator":
        from .simulator import SimulatorSource

        return SimulatorSource(config)
    if kind == "modbus_tcp":
        from .modbus_tcp import ModbusTcpSource

        return ModbusTcpSource(config)
    if kind == "opcua":
        from .opcua import OpcUaSource

        return OpcUaSource(config)
    if kind == "csv":
        from .csv_tail import CsvTailSource

        return CsvTailSource(config)
    raise ValueError(f"unknown source type {kind!r} (simulator | modbus_tcp | opcua | csv)")


__all__ = ["DataSource", "create_source"]
