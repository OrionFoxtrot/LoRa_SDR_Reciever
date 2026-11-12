"""Bounded operator state and exports from the receiver's committed packet journal."""
from collections import deque
import csv
from dataclasses import dataclass, field
from itertools import islice
import json
import os
from pathlib import Path
import tempfile

from reports import FIELDS

LOG_LIMIT = 300
HISTORY_LIMIT = 12
STALE_SECONDS = 30  # One nominal DeSCENT reporting period.

SCALARS = {
    "latitude_deg": "gps", "longitude_deg": "gps", "gps_altitude_msl_m": "gps",
    "state_of_charge_percent": "state_of_charge", "temperature_C": "environmental",
    "pressure_hPa": "environmental", "humidity_percent": "environmental",
    "environmental_altitude_m": "environmental",
}
VECTORS = {
    "linear_acceleration_mps2": ("linear_acceleration", "xyz"),
    "gyroscope_deg_s": ("gyroscope", "xyz"), "magnetic_field_uT": ("magnetometer", "xyz"),
    "quaternion_ijkr": ("quaternion", "ijkr"),
}
VALID = ("gps", "state_of_charge", "environmental", "linear_acceleration", "gyroscope",
         "magnetometer", "quaternion", "all_data_fresh")
ENGINEERING = [*SCALARS, *(f"{k}_{axis}" for k, (_, axes) in VECTORS.items() for axis in axes),
               *(f"valid_{v}" for v in VALID)]


def crc(value):
    if value in ("True", True):
        return True
    if value in ("False", False):
        return False
    return None


def engineering(data):
    """Use empty CSV cells for absent or invalid sensor groups, keeping actual zeros."""
    valid = data.get("valid", {})
    out = {f"valid_{key}": valid.get(key) for key in VALID}
    for k, group in SCALARS.items():
        if valid.get(group):
            out[k] = data.get(k)
    for k, (group, axes) in VECTORS.items():
        if valid.get(group):
            out.update((f"{k}_{a}", v) for a, v in zip(axes, data.get(k, ())))
    return out


@dataclass
class Packet:
    row: dict
    t: float
    fc: int
    sf: int
    node: int | None
    data: dict

    @classmethod
    def read(cls, row):
        node = row.get("chipsat_id")
        return cls(row, float(row["capture_s"]), int(row["carrier_hz"]), int(row["sf"]),
                   int(node) if node not in ("", None) else None,
                   json.loads(row["telemetry_json"]) if row.get("telemetry_json") else {})

    @property
    def trusted(self):
        return (self.node is not None and crc(self.row.get("application_crc_ok")) is True
                and crc(self.row.get("phy_crc_ok")) is not False)


@dataclass(frozen=True)
class Filters:
    node: str = ""
    fc: int | None = None
    sf: int | None = None

    def matches(self, packet):
        return ((not self.node or (self.node.isdecimal() and (self.node.lstrip("0") or "0") == str(packet.node)))
                and (self.fc is None or self.fc == packet.fc)
                and (self.sf is None or self.sf == packet.sf))


@dataclass
class Node:
    count: int = 0
    latest: Packet | None = None
    position: Packet | None = None
    power: Packet | None = None
    history: list = field(default_factory=list)

    def hear(self, p):
        self.count += 1
        if self.latest is None or self.latest.t <= p.t:
            self.latest = p
        valid = p.data.get("valid", {})
        if valid.get("gps") and (self.position is None or self.position.t <= p.t):
            self.position = p
        if valid.get("state_of_charge") and (self.power is None or self.power.t <= p.t):
            self.power = p
        self.history.append(p)
        self.history.sort(key=lambda pkt: pkt.t, reverse=True)
        del self.history[HISTORY_LIMIT:]


class Fleet:
    def __init__(self):
        self.nodes = {}
        self.log = deque(maxlen=LOG_LIMIT)
        self.channels = {}
        self.total = 0

    def ingest(self, row):
        p = Packet.read(row)
        self.total += 1
        self.log.append((self.total, p))
        key = (p.fc, p.sf)
        count, last = self.channels.get(key, (0, p.t))
        self.channels[key] = count+1, max(last, p.t)
        if p.trusted:
            if p.node not in self.nodes:
                self.nodes[p.node] = Node()
            self.nodes[p.node].hear(p)

    def visible(self, filters, key, reverse):
        numbers = {i: n for i, n in self.nodes.items() if filters.matches(n.latest)}
        def sort_value(i):
            n = numbers[i]
            return {"id": i, "last": n.latest.t, "age": -n.latest.t, "count": n.count,
                    "carrier": n.latest.fc, "sf": n.latest.sf,
                    "soc": n.power.data["state_of_charge_percent"] if n.power else None}[key]
        # Unknown SoC stays at the bottom in either direction. ID breaks ties.
        ordered = sorted(numbers)
        known = [i for i in ordered if sort_value(i) is not None]
        return sorted(known, key=sort_value, reverse=reverse) + [i for i in ordered if sort_value(i) is None]


class JournalCursor:
    def __init__(self):
        self.offset = self.count = 0
        self.fields = None

    def read(self, path, committed, limit=256):
        size = min(limit, committed-self.count)
        if size <= 0:
            return []
        with path.open(encoding="utf-8", newline="") as stream:
            stream.seek(self.offset)
            rows = csv.DictReader(iter(stream.readline, ""), fieldnames=self.fields)
            batch = list(islice(rows, size))
            self.fields = rows.fieldnames
            self.offset = stream.tell()
        self.count += len(batch)
        return batch


FLEET_FIELDS = ["packets_received", "last_valid_position_capture_s", "last_valid_latitude_deg",
                "last_valid_longitude_deg", "last_valid_altitude_msl_m", "last_soc_capture_s",
                "last_available_soc_percent"]


def export_csv(journal, count, destination, kind="all", filters=Filters()):
    """Stream one committed prefix. An export can safely run alongside live capture."""
    if kind not in ("all", "filtered", "fleet"):
        raise ValueError("Unknown export format")
    destination = Path(destination)
    if destination.suffix.lower() != ".csv":
        raise ValueError("Choose a .csv filename.")
    protected={"packets.csv", "packets_primary.csv", "packets_residual.csv", "matches.csv"}
    if destination.name.casefold() in protected and (destination.parent/"summary.json").is_file():
        raise ValueError("Choose an export filename different from a receiver journal.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    cols = [*FIELDS, *ENGINEERING]
    if kind == "fleet":
        cols += FLEET_FIELDS
    written = 0
    tmp = None
    try:
        with journal.open(encoding="utf-8-sig", newline="") as src, tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8-sig", newline="", prefix=".export-", suffix=".tmp",
                dir=destination.parent, delete=False) as out:
            tmp = Path(out.name)
            writer = csv.DictWriter(out, cols)
            writer.writeheader()
            nodes = {}
            read = 0
            for row in islice(csv.DictReader(src), count):
                read += 1
                p = Packet.read(row)
                if kind == "fleet":
                    if p.trusted:
                        if p.node not in nodes:
                            nodes[p.node] = Node()
                        nodes[p.node].hear(p)
                elif kind == "all" or filters.matches(p):
                    writer.writerow({**row, **engineering(p.data)})
                    written += 1
            if read != count:
                raise OSError("The packet journal ended before the captured export boundary.")
            for i in sorted(nodes):
                n = nodes[i]
                pos = n.position.data if n.position else {}
                writer.writerow({**n.latest.row, **engineering(n.latest.data), "packets_received": n.count,
                    "last_valid_position_capture_s": n.position.t if n.position else None,
                    "last_valid_latitude_deg": pos.get("latitude_deg"), "last_valid_longitude_deg": pos.get("longitude_deg"),
                    "last_valid_altitude_msl_m": pos.get("gps_altitude_msl_m"),
                    "last_soc_capture_s": n.power.t if n.power else None,
                    "last_available_soc_percent": n.power.data["state_of_charge_percent"] if n.power else None})
                written += 1
        os.replace(tmp, destination)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    return written

