"""Run the repeatable offline bench campaign from the normal receiver interfaces."""
import argparse
from collections import defaultdict
import csv
import hashlib
import json
from pathlib import Path
import platform
import sys
import unittest
from config import ROOT,profile_from_settings,read_profile
from fixtures import NINE_BRANCH_PROFILE,Event,GOLD,fleet_events,generate,payload,power_events
from reports import directory,score,write_json
from run import live,replay
from tests.loopback import BenchSocket


def result(run,tx):
    s=json.loads((run/"summary.json").read_text())
    v=score(run,tx,tolerance_s=.015)
    return {**{k:s[k] for k in ("run_directory","evidence","capture_seconds","processing_seconds",
             "elapsed_seconds","processing_x_realtime","dropped_samples","clipped_samples",
             "peak_backlog_s","backlog_growing","backlog_slope_s_per_s","capture_sha256")},
            **{k:v[k] for k in ("offered","matched","missed","false_decodes","duplicate_decodes",
                "rejected_crc_or_unverified","periods","max_abs_timing_error_ms","fraction_complete_periods_at_least_80")}}


def sweep_counts(run,truth):
    with truth.open(newline="") as f:
        rows=list(csv.DictReader(f))
    with (Path(run)/"matches.csv").open(newline="") as f:
        hits={int(r["tx_row"])-1 for r in csv.DictReader(f) if r["classification"]=="match"}
    counts=defaultdict(lambda:defaultdict(lambda:[0,0]))
    for i,row in enumerate(rows):
        db,kind,sf,_=row["case"].split("/")
        nums=counts[int(db)][f"{kind}_sf{sf}"]
        nums[0]+=int(i in hits)
        nums[1]+=1
    return dict(sorted(counts.items()))


def paced_check(p,out,report):
    iq=out/"iq"/"fleet100.iq"
    seconds=iq.stat().st_size/(2*p.sample_rate_hz)
    base=report["fleet_primary"]
    print(f"Playing the same {seconds:g} s through the live socket reader at 1 MS/s",flush=True)
    with BenchSocket(iq) as server:
        run=live(p,out/"paced_socket",seconds=seconds,connect=server.address,
                 evidence="synthetic 1x rtl_tcp loopback")
    paced=result(run,iq.with_suffix(".tx.csv"))
    report["paced_socket"]=paced
    report["rtl_tcp_commands"]=server.commands
    report["paced_recording_sidecar_sha256"]=hashlib.sha256((run/"capture.iq.json").read_bytes()).hexdigest()
    return (paced["capture_sha256"]==base["capture_sha256"] and paced["matched"]==base["matched"]
            and paced["false_decodes"]==0 and paced["duplicate_decodes"]==0 and paced["dropped_samples"]==0
            and paced["backlog_growing"] is False and server.commands==[(2,1000000),(1,p.center_hz),
                                                                        (5,0),(8,0),(3,1),(4,100)])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out",type=Path,default=ROOT/"runs"/"acceptance")
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument("--quick",action="store_true",help="unit, independent PHY, all concurrent branches")
    mode.add_argument("--field",action="store_true",help="synthetic 100-node staggered load, two-radio GPS and generic payloads")
    mode.add_argument("--paced-from",type=Path,help="repeat the 1x socket check using an existing acceptance run")
    args=parser.parse_args()
    if args.field:
        from field_acceptance import campaign
        campaign(args.out)
        return 0
    p=read_profile(NINE_BRANCH_PROFILE)
    out=args.paced_from.resolve() if args.paced_from else directory(args.out)
    print("Acceptance directory",out,flush=True)
    report=dict(python=sys.version,host=platform.platform(),profile=p.report(),gates={},
                physical_rtl_sdr="unverified",flight_register_readback="unverified")
    def save():
        write_json(out/"acceptance.json",report)
    def gate(key,ok):
        report["gates"][key]=bool(ok)
        print(f"{'PASS' if ok else 'FAIL'}   {key}",flush=True)
        save()
    paced_gate="paced live path records identical IQ and recovers baseline results"
    if args.paced_from:
        report=json.loads((out/"acceptance.json").read_text())
        p=profile_from_settings(report["profile"])
        gate(paced_gate,paced_check(p,out,report))
        report["passed"]=all(report["gates"].values())
        save()
        return 0 if report["passed"] else 1
    def evaluate(name,iq,sic=False):
        run=replay(p,iq,out/name,sic=sic)
        r=result(run,iq.with_suffix(".tx.csv"))
        report[name]=r
        print(f"{name}: {r['matched']}/{r['offered']} in {r['processing_seconds']:.2f} processing s",flush=True)
        save()
        return r

    with (out/"unit_tests.txt").open("w",encoding="utf-8") as log:
        suite=unittest.defaultTestLoader.discover(str(ROOT/"tests"))
        unit=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
    report["unit_tests"]=unit.testsRun
    gate("stream, codec, telemetry, gain, settings and scoring tests",unit.wasSuccessful())
    iq=ROOT/"examples"/"epfl_bench.iq"
    independent=evaluate("epfl_interop",iq)
    gate("three independent isolated packets and equal SF11/SF12 overlap",
         independent["matched"]==5 and independent["false_decodes"]==0 and independent["duplicate_decodes"]==0)
    fixture=out/"iq"
    iq=fixture/"concurrent_branches.iq"
    simultaneous=[Event(.124723,fc,sf,payload(1+fi*3+si,1),.13,(-2345,1713,423)[si],
                        (32,-45,38)[si],si+.3*fi,"all branches concurrent")
                  for fi,fc in enumerate(p.carriers_hz) for si,sf in enumerate(p.spreading_factors)]
    generate(iq,p,simultaneous,3.51)
    concurrent=evaluate("concurrent_branches",iq)
    gate("all concurrent branches, byte- and CRC-valid",
         concurrent["matched"]==len(simultaneous) and concurrent["false_decodes"]==0
         and concurrent["duplicate_decodes"]==0)
    if not args.quick:
        iq=fixture/"near_far.iq"
        events,seconds=power_events(p)
        generate(iq,p,events,seconds)
        base=evaluate("power_primary",iq)
        sic=evaluate("power_sic",iq,True)
        base_counts=sweep_counts(base["run_directory"],iq.with_suffix(".tx.csv"))
        sic_counts=sweep_counts(sic["run_directory"],iq.with_suffix(".tx.csv"))
        report["power_sweep"]={str(k):dict(primary=v,sic=sic_counts[k]) for k,v in base_counts.items()}
        all_controls=all(v[0]==v[1] for counts in base_counts.values() for key,v in counts.items() if "control" in key)
        gate("both weaker-alone controls across signed power sweep",all_controls)
        gate("SIC recovers extra overlap packets on identical samples",
             base["capture_sha256"]==sic["capture_sha256"] and sic["matched"]>base["matched"]
             and sic["false_decodes"]==0 and sic["duplicate_decodes"]==0)
        iq=fixture/"fleet100.iq"
        events,seconds=fleet_events(p)
        generate(iq,p,events,seconds,seed=8521)
        base=evaluate("fleet_primary",iq)
        sic=evaluate("fleet_sic",iq,True)
        gate("300 offered fleet transmissions scored on identical IQ",
             base["offered"]==sic["offered"]==300 and base["capture_sha256"]==sic["capture_sha256"])
        gate(paced_gate,paced_check(p,out,report))
    report["passed"]=all(report["gates"].values())
    save()
    print(out/"acceptance.json",flush=True)
    return 0 if report["passed"] else 1


if __name__=="__main__":
    sys.exit(main())
