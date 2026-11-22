"""Blind IQ acceptance for the proposed load. All start times here are synthetic."""
import argparse
import binascii
from collections import Counter
import csv
import json
import math
from pathlib import Path
import struct
from unittest.mock import patch

from config import ROOT, read_profile
from fixtures import NINE_BRANCH_PROFILE, Event, generate, payload
from phy import waveform
from reports import directory, score, write_json
from run import replay


def science(node, seq, *, moving=True, fix=True):
    data=bytearray(payload(node,seq)[:53])
    lat=42.45+(node%12)*.00035+(seq*.00050 if moving else 0)
    lon=-76.48+(node%9)*.00025+(math.sin(seq*.6)*.00085 if moving else 0)
    struct.pack_into("<4i",data,0,round(lat*1e7),round(lon*1e7),1_230_000+seq*4000,122500+seq*410)
    if not fix:
        data[52]&=~0x10
    return bytes(data)+binascii.crc_hqx(data,0xffff).to_bytes(2,"little")


def scheduled_load(p):
    events=[]
    for side,carrier in enumerate(p.fleet_carriers_hz):
        node_offset=1+side*50
        for sf,count,phase in ((10,30,.10),(11,12,.18),(12,8,.26)):
            for slot in range(count):
                node=node_offset+slot
                for k in range(3):
                    # Independent fixture phases only. No event list is passed to the receiver.
                    t=30*k+30*slot/count+phase+.032*side
                    level=.15*(.77+(.29*((node*7+k)%13)/12))
                    events.append(Event(t,carrier,sf,science(node,k),level, (node*97)%6900-3200,18.,
                                        (node%8)*.3,"generated staggered load"))
            node_offset+=count
    return events,93.


def bench_demo(p,out):
    center=p.center_hz
    events=[]
    for k in range(10):
        for node,sf,shift in ((249,11,0.),(250,12,.09)):
            data=science(node,k,moving=node==249,fix=(k not in (4,5,9) or node==250))
            level=(.23-.015*k) if node==249 else .13
            events.append(Event(.20+3.85*k+shift,center,sf,data,level,1000*math.sin(k*.6),0.,.5,"bench pair"))
    for k,data in enumerate((b"hello",b"\x00\xff\x00A\x7f",bytes(range(55)),b"without CRC",b"bad PHY")):
        events.append(Event(39.2+k*1.15,center,10,data,.15,200,0.,.2,"generic",crc=k!=3))
    ref=out/"bad-phy.cf32"
    with patch("lora_phy.tx.calc_crc",return_value=(0x19,0x33)):
        waveform(p,10,b"bad PHY",3,True,250000).astype("complex64").tofile(ref)
    events[-1].reference=ref
    name=out/"two-radio-demo.iq"
    generate(name,p,events,45.3,description="Synthetic moving and stationary bench radios plus generic PHY diagnostics")
    return name


def campaign(parent):
    out=directory(parent)
    print("Field acceptance",out,flush=True)
    p=read_profile(NINE_BRANCH_PROFILE)
    bench=bench_demo(p,out)
    from mission import Mission
    from fleet_view import Packet,crc
    benchrun=replay(p,bench,out/"bench",mission=Mission(size=2,target=2,period=10),name="Two-radio bench demo")
    def read(path):
        with path.open(encoding="utf-8-sig",newline="") as f:
            return list(csv.DictReader(f))
    bench_packets=[Packet.read(row) for row in read(benchrun/"packets.csv")]
    truth=read(bench.with_suffix(".tx.csv"))
    expected=Counter(row["payload_hex"] for row in truth if row["phy_crc"]=="True" and bytes.fromhex(row["payload_hex"])!=b"bad PHY")
    passed=Counter(pkt.row["payload_hex"] for pkt in bench_packets if crc(pkt.row["phy_crc_ok"]) is True)
    assert passed==expected and {pkt.node for pkt in bench_packets if pkt.trusted}=={249,250}
    assert [crc(pkt.row["phy_crc_ok"]) for pkt in bench_packets if bytes.fromhex(pkt.row["payload_hex"])==b"bad PHY"]==[False]
    assert [crc(pkt.row["phy_crc_ok"]) for pkt in bench_packets if bytes.fromhex(pkt.row["payload_hex"])==b"without CRC"]==[None]
    generic=dict(journal=len(bench_packets),phy_valid=sum(passed.values()),trusted=sum(pkt.trusted for pkt in bench_packets),
                 unrelated_55_application_crc=next(crc(pkt.row["application_crc_ok"]) for pkt in bench_packets
                     if pkt.row["payload_hex"]==bytes(range(55)).hex()),failed_phy=1,absent_phy=1)
    events,duration=scheduled_load(p)
    iq=out/"staggered100.iq"
    generate(iq,p,events,duration,description="Synthetic staggered 100-node load; generated phases; 30/12/8 per carrier")
    results={}
    for kind in ("primary","sic"):
        folder=replay(p,iq,out/kind,sic=kind=="sic",name="100-radio staggered IQ")
        s=json.loads((folder/"summary.json").read_text())
        v=score(folder,iq.with_suffix(".tx.csv"))
        periods=[{"start_s":w["start_s"],"ids":w["unique_valid_chipsats"],"target_met":w["reached_80"]}
                 for w in v["periods"] if w["complete"]]
        results[kind]=dict(folder=str(folder.resolve()),sha256=s["capture_sha256"],periods=periods,
                           offered=v["offered"],matched=v["matched"],false=v["false_decodes"],duplicates=v["duplicate_decodes"],
                           processing_s=s["processing_seconds"],dropped_samples=s["dropped_samples"])
        assert [w["heard"] for w in s["mission_periods"] if not w["partial"]]==[w["ids"] for w in periods]
        print(kind,periods,flush=True)
    assert results["primary"]["sha256"]==results["sic"]["sha256"]
    assert all(v["offered"]==300 and len(v["periods"])==3 for v in results.values())
    assert all(v["false"]==0 and v["duplicates"]==0 for v in results.values())
    write_json(out/"field_acceptance.json",dict(bench=str(benchrun.resolve()),timing="generated independently of flight timings",
                                                generic_bench=generic,**results))
    print(out/"field_acceptance.json",flush=True)
    return out


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out",type=Path,default=ROOT/"validation"/"field")
    campaign(parser.parse_args().out)
