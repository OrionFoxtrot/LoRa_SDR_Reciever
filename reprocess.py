"""Offline SIC comparisons. The original run and its capture are read-only inputs."""
from collections import defaultdict
import csv
import hashlib
import json
from pathlib import Path

from config import EventIQLimits, profile_from_settings
from fleet_view import Packet,crc
from mission import PeriodBook, saved_mission
from reports import directory, score, write_json, write_packets
from run import replay
from sources import Layout
from sic import repeat_window


def capture_for(folder,summary):
    local=folder/"capture.iq"
    if local.is_file():
        return local
    if summary.get("mode")=="replay" and summary.get("capture_path"):
        original=Path(summary["capture_path"])
        for p in (original,folder/original.name):
            if p.is_file():
                return p
    return None


def load_valid(path,end):
    with path.open(encoding="utf-8-sig",newline="") as stream:
        return [p for row in csv.DictReader(stream) if crc(row["phy_crc_ok"]) is True and (p:=Packet.read(row)).t<end]


def additions(base,other):
    groups=defaultdict(list)
    for p in base:
        groups[p.fc,p.sf,p.row["payload_hex"]].append(int(p.row["start_sample"]))
    extra=[]
    for p in other:
        group=groups.get((p.fc,p.sf,p.row["payload_hex"]),[])
        start=int(p.row["start_sample"])
        nearest=min(group,key=lambda x:abs(x-start),default=None)
        if nearest is not None and abs(start-nearest)<=repeat_window(p.row):
            group.remove(nearest)
        else:
            extra.append(p)
    return extra


def compare(primary,sic_folder,mission,duration,gaps,primary_journal="packets.csv",output=None):
    base=load_valid(primary/primary_journal,duration)
    final=load_valid(sic_folder/"packets.csv",duration)
    replay_primary=load_valid(sic_folder/"packets_primary.csv",duration)
    extra=additions(base,final)
    residual=additions(replay_primary,[p for p in extra if p.row["pass_name"]=="sic"])
    write_packets((output or sic_folder)/"sic_only.csv",(p.row for p in residual))
    books=[PeriodBook(mission),PeriodBook(mission)]
    for book,packets in zip(books,(base,final)):
        for pkt in packets:
            if pkt.trusted:
                book.hear(pkt)
    rows=[]
    for before,after in zip(*(b.report(duration,gaps) for b in books)):
        i=before["index"]
        a=set(books[0].heard.get(i,{}))
        b=set(books[1].heard.get(i,{}))
        if mission.roster:
            a.intersection_update(mission.roster)
            b.intersection_update(mission.roster)
        rows.append(dict(period=i+1,start_s=before["start_s"],end_s=before["end_s"],primary=len(a),with_sic=len(b),
                         additional_ids=sorted(b-a),lost_ids=sorted(a-b),partial=before["partial"],gap=before["gap"],
                         newly_met=not before["partial"] and not before["met"] and after["met"]))
    return dict(primary_run=str(primary.resolve()),comparison_run=str(sic_folder.resolve()),comparison_seconds=duration,
                primary_gap_ranges_s=gaps,
                primary_valid=sum(p.trusted for p in base),merged_valid=sum(p.trusted for p in final),
                additional_valid=sum(p.trusted for p in extra),additional_phy_valid=len(extra),sic_only=len(residual),
                sic_only_trusted=sum(p.trusted for p in residual),
                missing_from_replay=len(additions(final,base)),periods=rows,newly_meeting_target=sum(r["newly_met"] for r in rows))


def inspect_comparison(sic_folder,primary,mission,primary_journal="packets.csv"):
    """House additional reports in their own directory, including for a pre-existing SIC run."""
    s=json.loads((sic_folder/"summary.json").read_text(encoding="utf-8"))
    dest=directory(sic_folder/"comparisons")
    # Reference existing journals directly. Only the comparison CSV and JSON are new.
    report=compare(primary,sic_folder,mission,s["capture_seconds"],s.get("gap_ranges_s",[]),
                   primary_journal,output=dest)
    report["sic_only_file"]=str((dest/"sic_only.csv").resolve())
    write_json(dest/"comparison.json",report)
    return report


def reprocess(folder,stop,notify,mission=None):
    folder=Path(folder)
    summary=json.loads((folder/"summary.json").read_text(encoding="utf-8"))
    if summary.get("status") not in ("complete","stopped") or summary.get("sic_enabled"):
        raise ValueError("Choose a completed primary run.")
    iq=capture_for(folder,summary)
    if iq is None:
        raise ValueError("IQ was not saved or the original recording is unavailable.")
    p=profile_from_settings(summary["profile"])
    m=mission or saved_mission(folder,summary)
    layout=Layout.read(iq)
    if not layout.samples:
        raise ValueError("No IQ samples were saved for this run.")
    target=directory(folder/"analyses")
    write_json(target/"mission.json",m.dump())
    end=round(summary["capture_seconds"]*p.sample_rate_hz)
    previous = summary.get("event_iq", {})
    defaults = EventIQLimits()
    limits = EventIQLimits(previous.get("max_per_kind_per_hour",defaults.per_kind_hour),
                           previous.get("max_total_bytes",defaults.max_mb*1_000_000)//1_000_000)
    replay(p,iq,target.parent,folder=target,name=summary.get("run_name",folder.name)+" · SIC",
           mission=m,sic=True,stop=stop,notify=notify,end_sample=end,event_limits=limits)
    s=json.loads((target/"summary.json").read_text(encoding="utf-8"))
    if stop.is_set() or s["status"]!="complete":
        return None
    if summary.get("capture_sha256") and s["capture_sha256"]!=summary["capture_sha256"]:
        raise ValueError("The IQ fingerprint differs from the primary run.")
    if not s["sic"]["identical_input_bytes"]:
        raise ValueError("The primary and residual passes used different samples.")
    gaps=[*summary.get("gap_ranges_s",[]),*s.get("gap_ranges_s",[])]
    result=compare(folder,target,m,s["capture_seconds"],gaps)
    if summary.get("dropped_samples") and "gap_ranges_s" not in summary:
        result["original_gap_locations_unknown"]=True
    result["iq_sha256"]=s["capture_sha256"]
    result["same_recording_verified"]=bool(summary.get("capture_sha256"))
    result["original_capture_seconds"]=summary["capture_seconds"]
    original_score=folder/"score.json"
    if original_score.is_file():
        truth=json.loads(original_score.read_text(encoding="utf-8"))
        tx=Path(truth["transmit_log"])
        if tx.is_file() and hashlib.sha256(tx.read_bytes()).hexdigest()==truth.get("transmit_log_sha256"):
            score(target,tx,truth["tx_to_capture_offset_s"],truth["match_tolerance_s"])
    write_json(target/"comparison.json",result)
    return target
