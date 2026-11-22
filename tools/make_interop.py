"""Rebuild the shipped EPFL synthetic fixture with a separately installed radioconda."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import read_profile, ROOT
from fixtures import Event, GOLD, generate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True, type=Path, help="radioconda python.exe")
    parser.add_argument("--out", type=Path, default=ROOT/"examples"/"epfl_bench.iq")
    args = parser.parse_args()
    from fixtures import NINE_BRANCH_PROFILE
    profile = read_profile(NINE_BRANCH_PROFILE)
    a,b = profile.fleet_carriers_hz
    with tempfile.TemporaryDirectory() as tmp:
        env = os.environ.copy()
        env["APPDATA"] = tmp
        env["PATH"] = str(args.python.parent/"Library"/"bin")+os.pathsep+env["PATH"]
        files = []
        for i,sf in enumerate(profile.spreading_factors):
            ref = Path(tmp)/f"sf{sf}.c64"
            subprocess.run([str(args.python),"-B",str(ROOT/"tools/epfl_vector.py"),
                            str(ref),str(sf),GOLD[i].hex()],env=env,check=True,timeout=25)
            files.append(ref)
        events = [
            Event(.183317,a,10,GOLD[0],.19,3593,37,.4,"isolated SF10",reference=files[0]),
            Event(1.193137,a,11,GOLD[1],.19,-2711,-52,1.8,"isolated SF11",reference=files[1]),
            Event(3.317791,b,12,GOLD[2],.19,7801,68,2.5,"isolated SF12",reference=files[2]),
            Event(6.873717,a,11,GOLD[1],.19,-2711,-52,.8,"equal pair SF11",reference=files[1]),
            Event(6.873717,a,12,GOLD[2],.19,619,68,1.2,"equal pair SF12",reference=files[2])]
        generate(args.out,profile,events,10.25,description="EPFL gr-lora_sdr a8143cb independent TX at 250 kS/s; resampled, mixed and CU8 quantized")


if __name__ == "__main__":
    main()
