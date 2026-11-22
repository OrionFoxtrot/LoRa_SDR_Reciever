"""Operator hardware selection and the last front end actually used for live IQ."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from dataclasses import replace
from config import ROOT, front_end_settings, monitor_profile_name


FRONT_END_LABELS = {
    "lana": "Nooelec LaNA (USB, 20 dB)",
    "lana-wb": "Nooelec LaNA WB (USB, 19 dB)",
    "none": "None (antenna direct to SDR)",
    "custom": "Custom",
}


def preset(key):
    if key == "lana":
        return front_end_settings()
    if key == "lana-wb":
        return front_end_settings("Nooelec LaNA WB",19.,"USB")
    if key == "none":
        return front_end_settings("None")
    raise ValueError(f"Unknown front-end preset {key!r}.")


def from_fields(fields):
    return front_end_settings(fields["lna_model"],fields["nominal_gain_db"],fields["power_source"],
                              fields["filter"],fields.get("usb_supply") or "")


def preset_for(front):
    return next((key for key in FRONT_END_LABELS if key != "custom" and preset(key) == front),"custom")


def short_name(front):
    if not front:
        return "Front end not recorded"
    if front.get("lna_model") == "None":
        return "Antenna direct"
    return str(front.get("lna_model") or "Model unspecified")


def gain_reminder(previous, current, auto_gain):
    if previous and current and previous != current and not auto_gain:
        return ("Front end changed since the last live run. Rerun Auto Set before using fixed gain. "
                "Adding or removing the LaNA can shift the required tuner gain by about 20 dB.")
    return None


class FrontEndPreferences:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else ROOT/".receiver-settings.sqlite3"
        self._history_key = self._previous_front = None

    def read(self, key):
        if not self.path.is_file():
            return None
        try:
            with closing(sqlite3.connect(self.path)) as db:
                row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
                return json.loads(row[0]) if row else None
        except (sqlite3.Error, ValueError):
            return None

    def selected(self):
        saved = self.read("selection") or {}
        try:
            key = saved.get("choice","lana")
            return key, from_fields(saved["metadata"]) if key == "custom" else preset(key)
        except (KeyError, TypeError, ValueError, AttributeError):
            return "lana", preset("lana")

    def last_run(self, parent):
        previous = self.read("last_run")
        if previous:
            return previous
        # On upgrade, recover the last live setup from the selected run folder.
        # A recent hardware run with missing metadata leaves the comparison unknown.
        folder = Path(parent)
        try:
            key = (folder,folder.stat().st_mtime_ns)
        except OSError:
            return None
        if key != self._history_key:
            latest = ""
            self._previous_front = None
            for path in folder.glob("*/summary.json"):
                try:
                    summary = json.loads(path.read_text(encoding="utf-8-sig"))
                    if (summary.get("mode") != "live" or summary.get("evidence") != "live hardware"
                            or not summary.get("processed_samples")):
                        continue
                    stamp = summary.get("capture_start_utc") or summary.get("started_utc") or path.parent.name
                    if stamp >= latest:
                        latest, self._previous_front = stamp,summary.get("front_end")
                except (OSError, ValueError, TypeError, AttributeError):
                    continue
            self._history_key = key
        return self._previous_front

    def save(self, choice, front, *, received=False):
        values = {"selection":dict(choice=choice,metadata=front)}
        if received:
            values["last_run"] = front
        self.write(values)

    def carrier_profile(self, profile):
        saved = self.read("carrier_selection") or {}
        try:
            if saved:
                return replace(profile, center_hz=saved["center_hz"], carriers_hz=tuple(saved["carriers_hz"]),
                               spreading_factors=tuple(saved["spreading_factors"]),
                               monitor_only_hz=tuple(saved.get("monitor_only_hz",()))).checked()
            return profile
        except (KeyError,TypeError,ValueError,AttributeError):
            return profile

    def save_carriers(self, profile, name=None):
        value = dict(name=name or monitor_profile_name(profile.carriers_hz),center_hz=profile.center_hz,
                     carriers_hz=list(profile.carriers_hz),spreading_factors=list(profile.spreading_factors),
                     monitor_only_hz=list(profile.monitor_only_hz))
        if self.read("carrier_selection") != value:
            self.write({"carrier_selection":value})

    def write(self, values):
        try:
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                db.executemany("INSERT OR REPLACE INTO settings VALUES (?, ?)",
                               [(k,json.dumps(v,allow_nan=False)) for k,v in values.items()])
        except sqlite3.Error as exc:
            raise OSError(f"Could not save receiver settings to {self.path}: {exc}") from exc
