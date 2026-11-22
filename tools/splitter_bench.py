"""Score paired SDR/T-Beam receptions against the transmissions actually offered."""
import argparse
from collections import Counter, defaultdict
import csv
from itertools import chain
import json
import math
from pathlib import Path
from statistics import NormalDist

OUTCOMES = ("both_heard", "sdr_only", "tbeam_only", "neither_heard")


def wilson(successes, total):
    z = NormalDist().inv_cdf(.975)
    p = successes/total
    denominator = 1+z*z/total
    center = (p+z*z/(2*total))/denominator
    radius = z*math.sqrt(p*(1-p)/total+z*z/(4*total**2))/denominator
    return dict(count=successes, fraction=p, wilson95=[0. if not successes else center-radius,
                                                       1. if successes==total else center+radius])


def serial_rows(path):
    """CSV, JSON Lines, or timestamp-prefixed 'RX key=value ...' serial records."""
    with path.open(encoding="utf-8-sig", newline="") as file:
        lines = (line for line in file if line.strip() and not line.lstrip().startswith("#"))
        first = next(lines, None)
        if first is None:
            return
        lines = chain((first,), lines)
        if {"phy_crc_ok","payload_hex"} <= set(next(csv.reader([first]))):
            reader = csv.DictReader(lines)
            require(reader, {"phy_crc_ok","payload_hex"}, path)
            yield from reader
            return
        for line in lines:
            if line.lstrip().startswith("{"):
                row = json.loads(line)
            else:
                tokens = line.split()
                if "RX" not in tokens:
                    raise ValueError(f"{path}: unknown serial record {line.strip()!r}")
                row = dict(token.split("=",1) for token in tokens[tokens.index("RX")+1:])
            if "phy_crc_ok" not in row or "payload_hex" not in row:
                raise ValueError(f"{path}: each RX record needs phy_crc_ok and payload_hex")
            yield row


def require(reader, columns, path):
    missing = columns-set(reader.fieldnames or ())
    if missing:
        raise ValueError(f"{path}: missing columns {', '.join(sorted(missing))}")


def number(value, name):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def truth_table(path):
    rows = []
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        require(reader, {"attenuation_db"}, path)
        for r in reader:
            payload = bytes.fromhex(r.get("payload_hex") or r.get("scheduled_payload_hex") or "")
            if not payload:
                raise ValueError(f"{path}:{reader.line_num}: TX needs payload_hex")
            rows.append(dict(attenuation_db=number(r["attenuation_db"],"attenuation_db"),
                             tx_id=r.get("tx_id") or "", payload_hex=payload.hex(),
                             start_s=first(r,"start_s","scheduled_start_s","tx_start_s"),
                             carrier_hz=first(r,"carrier_hz","channel_frequency_hz"), sf=r.get("sf") or None))
    if not rows:
        raise ValueError("TX truth has no offered transmissions")
    return rows


def first(row, *names):
    return next((row[key] for key in names if row.get(key) not in (None,"")), None)


def heard(rows, truth, offset, tolerance):
    by_payload = defaultdict(list)
    for i, tx in enumerate(truth):
        by_payload[tx["payload_hex"]].append(i)
    hits, stats = set(), Counter()
    for row in rows:
        crc = str(row.get("phy_crc_ok", "")).strip().lower()
        if crc not in ("true","1","pass","ok"):
            if crc not in ("false","0","fail","","none","unknown"):
                raise ValueError(f"Unrecognized PHY CRC value {crc!r}")
            stats["crc_failed_or_unverified"] += 1
            continue
        payload = bytes.fromhex(row.get("payload_hex") or "")
        if not payload:
            raise ValueError("A PHY-valid record needs the received payload bytes")
        candidates = list(by_payload.get(payload.hex(), ()))
        for key, value in (("tx_id",row.get("tx_id")), ("sf",row.get("sf")),
                           ("carrier_hz",first(row,"carrier_hz","channel_frequency_hz"))):
            if value not in (None,""):
                candidates = [i for i in candidates if truth[i][key] in (None,"") or str(truth[i][key])==str(value)]
        if offset is not None:
            stamp = first(row, "capture_s", "rx_start_s")
            if stamp is None or any(truth[i]["start_s"] is None for i in candidates):
                raise ValueError("Clock matching needs RX capture_s or rx_start_s and TX start_s")
            t = number(stamp, "receive time")-offset
            candidates = [i for i in candidates if abs(t-number(truth[i]["start_s"],"TX start")) <= tolerance]
        if len(candidates) > 1:
            raise ValueError(f"Ambiguous payload {payload.hex()}: use unique TX bytes, tx_id or aligned timestamps")
        if not candidates:
            stats["unmatched_valid"] += 1
        elif candidates[0] in hits:
            stats["duplicates"] += 1
        else:
            hits.add(candidates[0])
    return hits, dict(stats)


def compare(tx, sdr, tbeam, sdr_offset=None, tbeam_offset=None, tolerance=.05):
    if not math.isfinite(tolerance) or tolerance <= 0 or any(
            v is not None and not math.isfinite(v) for v in (sdr_offset,tbeam_offset)):
        raise ValueError("Use finite clock offsets and a positive finite time tolerance")
    truth = truth_table(tx)
    with sdr.open(encoding="utf-8-sig", newline="") as file:
        journal = csv.DictReader(file)
        require(journal, {"phy_crc_ok","payload_hex"}, sdr)
        s_hits, s_stats = heard(journal, truth, sdr_offset, tolerance)
    t_hits, t_stats = heard(serial_rows(tbeam), truth, tbeam_offset, tolerance)
    groups = defaultdict(Counter)
    joined = []
    for i, row in enumerate(truth):
        outcome = OUTCOMES[(0 if i in s_hits else 2)+(0 if i in t_hits else 1)]
        groups[row["attenuation_db"]][outcome] += 1
        joined.append(dict(tx_row=i+1, **row, outcome=outcome))
    steps = []
    for db, counts in sorted(groups.items()):
        n = sum(counts.values())
        steps.append(dict(attenuation_db=db, offered=n, **{k:wilson(counts[k],n) for k in OUTCOMES},
                          sdr_heard=wilson(counts["both_heard"]+counts["sdr_only"],n),
                          tbeam_heard=wilson(counts["both_heard"]+counts["tbeam_only"],n)))
    return dict(steps=steps, sdr_log=s_stats, tbeam_log=t_stats), joined


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    for name in ("tx","sdr","tbeam"):
        ap.add_argument(name,type=Path)
    ap.add_argument("--out",type=Path,help="write JSON report (default stdout)")
    ap.add_argument("--joined",type=Path,help="write one decision per TX to CSV")
    ap.add_argument("--sdr-offset-s",type=float,help="SDR clock at TX time zero, enables clock matching")
    ap.add_argument("--tbeam-offset-s",type=float,help="T-Beam clock at TX time zero, enables clock matching")
    ap.add_argument("--tolerance-s",type=float,default=.05)
    args = ap.parse_args()
    try:
        report, joined = compare(args.tx,args.sdr,args.tbeam,args.sdr_offset_s,args.tbeam_offset_s,args.tolerance_s)
        data = json.dumps(report,indent=2,allow_nan=False)
        if args.out:
            args.out.write_text(data+"\n",encoding="utf-8")
        else:
            print(data)
        if args.joined:
            with args.joined.open("w",encoding="utf-8",newline="") as file:
                writer=csv.DictWriter(file,joined[0].keys())
                writer.writeheader()
                writer.writerows(joined)
    except (OSError,ValueError,TypeError) as exc:
        ap.error(str(exc))


if __name__ == "__main__":
    main()
