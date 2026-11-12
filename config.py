"""Receiver settings. Frequencies are absolute RF Hz, time is in complex samples."""
from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_PROFILE = ROOT / "profiles" / "descent.json"


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
