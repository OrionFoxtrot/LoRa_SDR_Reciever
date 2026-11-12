"""Run export and transmit-log scoring. The decoder never imports transmit events."""
from collections import defaultdict
import csv
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
from telemetry import decode_descent_packet


FIELDS = ("start_sample capture_s carrier_hz sf bandwidth_hz sync_word explicit_header "
          "cr_denominator ldro preamble_symbols phy_crc_ok application_crc_ok chipsat_id "
          "packet_counter cfo_hz drift_hz_s clock_error_ppm preamble_power_dbfs pass_name payload_hex telemetry_json "
          "run_id run_name receive_utc timestamp_source").split()


def telemetry(row, pass_name):
    row.update(pass_name=pass_name, application_crc_ok=None, chipsat_id=None, packet_counter=None,
               telemetry_json=None)
    data = bytes.fromhex(row["payload_hex"])
    if len(data) == 55:
        try:
            decoded = decode_descent_packet(data)
        except ValueError:
            row["application_crc_ok"] = False
        else:
            row.update(application_crc_ok=True, chipsat_id=decoded["csid"],
                       packet_counter=decoded["packet_counter"], telemetry_json=json.dumps(decoded))
    return row


def write_json(path, obj):
    path = Path(path)
    tmp = path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(obj, indent=2, allow_nan=False)+"\n", encoding="utf-8")
    tmp.replace(path)


def write_packets(path, rows):
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def directory(parent):
    parent = Path(parent)
    parent.mkdir(exist_ok=True, parents=True)
    from tempfile import mkdtemp
    return Path(mkdtemp(prefix=datetime.now().strftime("%Y%m%d_%H%M%S_"), dir=parent))


def periods(timed_ids, duration, offered=None, coverage_start=0):
    groups = defaultdict(set)
    for t, node in timed_ids:
        if 0 <= t < duration:
            groups[math.floor(t/30)].add(node)
    rows = []
    for i in range(math.ceil(duration/30)):
        row = dict(start_s=i*30, complete=coverage_start <= i*30 and (i+1)*30 <= duration,
                   unique_valid_chipsats=len(groups[i]), reached_80=len(groups[i]) >= 80)
        if offered is not None:
            row["offered_chipsats"] = len(offered[i])
        rows.append(row)
    complete = [w for w in rows if w["complete"]]
    return dict(periods=rows, fraction_complete_periods_at_least_80=(
        sum(w["reached_80"] for w in complete)/len(complete) if complete else None))


def score(run, truth_path, offset_s=0., tolerance_s=.05):
    """Positive offset means TX time zero occurs later in the capture."""
    run = Path(run)
    summary = json.loads((run/"summary.json").read_text(encoding="utf-8"))
    if not math.isfinite(offset_s) or not math.isfinite(tolerance_s) or tolerance_s <= 0:
        raise ValueError("A finite clock offset and positive tolerance are required")
    with Path(truth_path).open(encoding="utf-8-sig", newline="") as f:
        offered = list(csv.DictReader(f))
    truth = []
    buckets = defaultdict(list)
    for r in offered:
        def first(*keys):
            return next((r[k] for k in keys if r.get(k) not in (None, "")), None)
        t = first("start_s", "scheduled_start_s", "tx_start_s")
        payload = first("payload_hex", "scheduled_payload_hex")
        if t is None or payload is None:
            raise ValueError("Transmit CSV needs start_s and payload_hex (see README for aliases)")
        data = bytes.fromhex(payload)
        if not data or not math.isfinite(float(t)) or float(t) < 0:
            raise ValueError("Invalid transmit timestamp or payload")
        node = first("node_id", "chipsat_id", "csid")
        if node is None and len(data) == 55:
            node = decode_descent_packet(data)["csid"]
        fc = first("carrier_hz", "channel_frequency_hz")
        sf = first("sf")
        event = dict(t=float(t), data=data.hex(), node=None if node is None else int(node),
                     fc=int(fc) if fc else None, sf=int(sf) if sf else None)
        buckets[event["data"]].append(len(truth))
        truth.append(event)
    with (run/"packets.csv").open(encoding="utf-8-sig", newline="") as f:
        received = list(csv.DictReader(f))
    candidates = defaultdict(list)
    for i, r in enumerate(received):
        if r["phy_crc_ok"] != "True" or r["application_crc_ok"] == "False":
            continue
        for j in buckets[r["payload_hex"]]:
            e = truth[j]
            dt = abs(float(r["capture_s"])-offset_s-e["t"])
            if (dt <= tolerance_s and e["fc"] in (None, int(r["carrier_hz"]))
                    and e["sf"] in (None, int(r["sf"]))):
                candidates[i].append((dt, j))
    # Maximum cardinality, then minimum timing error within each byte value.
    # A repeated, identical payload can otherwise steal its neighbor's only match.
    import numpy as np
    from scipy.optimize import linear_sum_assignment
    rx_by_payload = defaultdict(list)
    for i in candidates:
        rx_by_payload[received[i]["payload_hex"]].append(i)
    matched = {}
    for indices in rx_by_payload.values():
        events = sorted({j for i in indices for _,j in candidates[i]})
        cols = {j:c for c,j in enumerate(events)}
        penalty = (min(len(indices),len(cols))+1)*(1+tolerance_s)
        cost = np.full((len(indices),len(cols)),penalty)
        for k,i in enumerate(indices):
            for dt,j in candidates[i]:
                cost[k,cols[j]] = dt
        rows,columns = linear_sum_assignment(cost)
        for k,c in zip(rows,columns):
            if cost[k,c] <= tolerance_s:
                matched[indices[k]] = events[c]
    valid = [i for i,r in enumerate(received) if r["phy_crc_ok"] == "True" and r["application_crc_ok"] != "False"]
    # Use the transmit clock for scored periods, including any partial
    # leading/trailing period. Blind summary.json uses the capture clock.
    offered_ids = defaultdict(set)
    for e in truth:
        if e["node"] is not None:
            offered_ids[math.floor(e["t"]/30)].add(e["node"])
    timed_ids = [(truth[j]["t"],int(received[i]["chipsat_id"])) for i,j in matched.items()
                 if received[i]["application_crc_ok"] == "True"]
    dt_ms = [1000*(float(received[i]["capture_s"])-offset_s-truth[j]["t"]) for i,j in matched.items()]
    with (run/"matches.csv").open("w",newline="",encoding="utf-8") as out:
        writer = csv.writer(out)
        writer.writerow(("receiver_row","tx_row","classification","timing_error_ms"))
        valid_set = set(valid)
        for i,r in enumerate(received):
            if i in matched:
                j = matched[i]
                writer.writerow((i+1,j+1,"match",1000*(float(r["capture_s"])-offset_s-truth[j]["t"])))
            else:
                label = "rejected" if i not in valid_set else "duplicate" if candidates.get(i) else "false"
                writer.writerow((i+1,"",label,""))
    result = dict(transmit_log=str(Path(truth_path).resolve()),
                  transmit_log_sha256=hashlib.sha256(Path(truth_path).read_bytes()).hexdigest(),
                  capture_sha256=summary.get("capture_sha256"), tx_to_capture_offset_s=offset_s,
                  match_tolerance_s=tolerance_s, offered=len(truth), matched=len(matched),
                  missed=len(truth)-len(matched), false_decodes=sum(i not in candidates for i in valid),
                  duplicate_decodes=sum(i in candidates and i not in matched for i in valid),
                  rejected_crc_or_unverified=len(received)-len(valid),
                  max_abs_timing_error_ms=max(map(abs,dt_ms), default=None),
                  period_origin_capture_s=offset_s,
                  **periods(timed_ids,max(0.,summary["capture_seconds"]-offset_s),offered_ids,-offset_s))
    write_json(run/"score.json", result)
    return result
