"""Receiver settings. Frequencies are absolute RF Hz, time is in complex samples."""
from dataclasses import asdict, dataclass, replace
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_PROFILE = ROOT / "profiles" / "descent.json"
MONITOR_CENTER_HZ = 915_000_000
MONITOR_SFS = (10, 11, 12)
MONITOR_PROFILES = {
    "915 MHz only": (915_000_000,),
    "Fleet A/B": (914_875_000, 915_125_000),
    "Four-carrier": (914_700_000, 914_900_000, 915_100_000, 915_300_000),
}
MONITOR_CHOICES = (*MONITOR_PROFILES, "Custom")


def monitor_profile_name(carriers):
    tuned = tuple(sorted(carriers))
    return next((name for name, frequencies in MONITOR_PROFILES.items() if frequencies == tuned),
                "Custom")


def carrier_preset(profile, name):
    return replace(profile,center_hz=MONITOR_CENTER_HZ,carriers_hz=MONITOR_PROFILES[name],
                   spreading_factors=MONITOR_SFS,monitor_only_hz=())


def search_half_width(value):
    width = int(value)
    if not 10_000 <= width <= 30_000:
        raise ValueError("Enter a frequency search half-width from 10000 to 30000 Hz.")
    return width


@dataclass(frozen=True)
class EventIQLimits:
    per_kind_hour: int = 6
    max_mb: int = 1000  # decimal MB of event IQ and its sidecars

    def __post_init__(self):
        if any(type(n) is not int or n < 0 for n in (self.per_kind_hour,self.max_mb)):
            raise ValueError("Event IQ limits must be nonnegative whole numbers.")


def front_end_settings(lna_model="Nooelec LaNA", nominal_gain_db=20., power_source="USB",
                       rf_filter="None", usb_supply="Unspecified"):
    model = lna_model.strip()
    if not model:
        raise ValueError("Enter an LNA model or None.")
    if model.lower() == "none":
        return dict(lna_model="None", nominal_gain_db=0., power_source="None", filter=rf_filter.strip() or "None",
                    usb_supply=None)
    gain = float(nominal_gain_db)
    if not math.isfinite(gain):
        raise ValueError("Enter a finite nominal LNA gain in dB.")
    return dict(lna_model=model, nominal_gain_db=gain, power_source=power_source.strip() or "Unspecified",
                filter=rf_filter.strip() or "None", usb_supply=usb_supply.strip() or "Unspecified")


def describe_front_end(front_end):
    if not front_end:
        return "Front end not recorded"
    gain = front_end.get("nominal_gain_db")
    gain_text = f", {gain:g} dB nominal" if gain is not None else ""
    supply = front_end.get("usb_supply")
    power = front_end.get("power_source", "unspecified")
    return (f"LNA {front_end.get('lna_model', 'unspecified')}{gain_text} · power {power}"
            + (f" ({supply})" if supply else "") + f" · filter {front_end.get('filter', 'unspecified')}")


def carrier_plan_warnings(profile, roster):
    carriers = sorted(profile.carriers_hz)
    warnings = []
    assignments = {}
    for node, row in sorted(roster.items()):
        fc, sf = row.get("carrier_hz"), row.get("sf")
        fc = int(fc) if fc not in ("", None) else None
        sf = int(sf) if sf not in ("", None) else None
        assignments.setdefault((fc, sf), []).append(str(node))
    margin = profile.max_cfo_hz - 2_000
    for (fc, sf), nodes in assignments.items():
        reasons = []
        if fc is not None:
            distance = min(abs(fc - tuned) for tuned in carriers)
            if distance > margin:
                reasons.append(f"{fc/1e6:g} MHz is {distance/1000:g} kHz from the nearest carrier "
                               f"(coverage limit {margin/1000:g} kHz with 2 kHz margin)")
        if sf is not None and sf not in profile.spreading_factors:
            reasons.append(f"SF{sf} is disabled")
        if reasons:
            warnings.append(f"Roster ID(s) {', '.join(nodes)}: " + ". ".join(reasons) + ".")
    for i, low in enumerate(carriers):
        for high in carriers[i+1:]:
            if high-low >= 200_000:
                break
            warnings.append(f"{low/1e6:g} and {high/1e6:g} MHz are {(high-low)/1000:g} kHz apart. "
                            "Below 200 kHz spacing, a strong adjacent packet can mask a weaker one.")
    return warnings


@dataclass(frozen=True)
class Profile:
    center_hz: int
    sample_rate_hz: int
    carriers_hz: tuple
    spreading_factors: tuple
    bandwidth_hz: int
    sync_word: int
    explicit_header: bool
    coding_rate_denominator: int
    phy_crc: bool
    preamble_symbols: int
    ldro: str
    invert_iq: bool
    implicit_length: int
    max_cfo_hz: int
    monitor_only_hz: tuple = ()

    @property
    def fleet_carriers_hz(self):
        return tuple(fc for fc in self.carriers_hz if fc not in self.monitor_only_hz)

    def checked(self):
        fs, bw = self.sample_rate_hz, self.bandwidth_hz
        if fs != 1_000_000 or bw <= 0 or fs % (2*bw):
            raise ValueError("Use 1 MS/s and a bandwidth with integer Fs/(2*BW).")
        if not self.carriers_hz or len(set(self.carriers_hz)) != len(self.carriers_hz):
            raise ValueError("Provide distinct carrier frequencies.")
        for fc in self.carriers_hz:
            if not 0 < fc < 2**32 or abs(fc-self.center_hz) + bw/2 + self.max_cfo_hz > fs/2:
                raise ValueError(f"{fc} Hz plus occupied bandwidth and CFO margin exceeds the capture.")
        if not 0 < self.max_cfo_hz < bw/4:
            raise ValueError("max_cfo_hz must be positive and less than BW/4.")
        if (not self.spreading_factors or len(set(self.spreading_factors)) != len(self.spreading_factors)
                or any(sf not in range(7, 13) for sf in self.spreading_factors)):
            raise ValueError("Choose distinct SFs in the range 7 through 12.")
        if self.ldro not in ("auto", "on", "off") or not 0 <= self.sync_word <= 255:
            raise ValueError("Invalid LDRO or sync word.")
        if not 6 <= self.preamble_symbols <= 128 or self.coding_rate_denominator not in range(5, 9):
            raise ValueError("Use 6–128 preamble symbols and coding rate 4/5 through 4/8.")
        if not 1 <= self.implicit_length <= 255:
            raise ValueError("Invalid implicit payload length.")
        return self

    def de(self, sf):
        return (2**sf / self.bandwidth_hz > .016) if self.ldro == "auto" else self.ldro == "on"

    def tuning(self, center):
        return replace(self, center_hz=int(center)).checked()

    def report(self):
        result = asdict(self)
        result["ldro_by_sf"] = {sf: self.de(sf) for sf in self.spreading_factors}
        return result


def read_profile(path=DEFAULT_PROFILE):
    with Path(path).open(encoding="utf-8-sig") as f:
        return profile_from_settings(json.load(f))


def profile_from_settings(settings):
    fields = dict(settings)
    fields.pop("ldro_by_sf", None)  # derived field in saved run profiles
    fields["monitor_only_hz"] = tuple(fields.get("monitor_only_hz", ()))
    for name in ("carriers_hz", "spreading_factors"):
        fields[name] = tuple(fields[name])
    if isinstance(fields["sync_word"], str):
        fields["sync_word"] = int(fields["sync_word"], 0)
    return Profile(**fields).checked()
