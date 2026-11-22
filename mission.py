"""Capture-clock coverage, independent of transmission order or expected transmit slots."""
from collections import defaultdict
from bisect import bisect_right, insort
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
import csv
import json
import math
from pathlib import Path


@dataclass(frozen=True)
class Mission:
    size: int = 100
    period: float = 30.
    target: int = 80
    epoch: float | None = None  # capture seconds at period zero
    alignment: str = "capture-aligned"
    station: tuple | None = None
    roster: dict = field(default_factory=dict)

    def checked(self):
        if not (1 <= self.target <= self.size <= 256) or not math.isfinite(self.period) or self.period <= 0:
            raise ValueError("Set a positive period and a target between one and the fleet size (up to 256).")
        if self.epoch is not None and not math.isfinite(self.epoch):
            raise ValueError("The period epoch must be finite.")
        if self.station is not None and not coordinates(*self.station):
            raise ValueError("Enter a station latitude from −90 to 90 and longitude from −180 to 180.")
        return self

    def dump(self):
        return asdict(self)

    @classmethod
    def read(cls, data):
        d=dict(data or {})
        d["roster"]={int(k):v for k,v in d.get("roster",{}).items()}
        return cls(**d).checked()

    @property
    def origin(self):
        return self.epoch or 0.

    def index(self,t):
        return math.floor((t-self.origin)/self.period)

    def start(self,i):
        return self.origin+i*self.period

    def aligned(self, meta, tx=None):
        if self.epoch is not None:
            return self
        for field,label in (("mission_epoch_capture_s","mission-aligned"),("tx_epoch_capture_s","TX-aligned")):
            if meta.get(field) is not None:
                return replace(self,epoch=float(meta[field]),alignment=label).checked()
        recorded=meta.get("mission") or {}
        if recorded.get("epoch") is not None:
            return replace(self,epoch=float(recorded["epoch"]),
                           alignment=recorded.get("alignment") or "mission-aligned").checked()
        start=meta.get("capture_start_utc") or meta.get("started_utc")
        for key,label in (("mission_epoch_utc","mission-aligned"),("tx_epoch_utc","TX-aligned")):
            if start and meta.get(key):
                a,b=datetime.fromisoformat(meta[key]),datetime.fromisoformat(start)
                if a.tzinfo and b.tzinfo:
                    return replace(self,epoch=(a-b).total_seconds(),alignment=label).checked()
        return replace(self,epoch=float(tx),alignment="TX-aligned").checked() if tx is not None else self


def read_roster(path):
    entries={}
    with Path(path).open(encoding="utf-8-sig",newline="") as f:
        for row in csv.DictReader(f):
            node=int(row.get("chipsat_id") or row.get("node_id") or row.get("csid"))
            if not 0<=node<=255 or node in entries:
                raise ValueError("Roster IDs must be distinct integers from 0 to 255.")
            fc=row.get("carrier_hz") or row.get("channel_frequency_hz")
            sf=row.get("sf")
            if (fc and not 0<int(fc)<2**32) or (sf and int(sf) not in range(7,13)):
                raise ValueError(f"Invalid carrier frequency or SF for roster ID {node}.")
            entries[node]={"carrier_hz":int(fc) if fc else None,"sf":int(sf) if sf else None}
    return entries


def coordinates(lat,lon):
    return lat is not None and lon is not None and -90<=lat<=90 and -180<=lon<=180


def valid_fix(p):
    d=p.data
    return bool(d.get("valid",{}).get("gps") and coordinates(d.get("latitude_deg"),d.get("longitude_deg")))


def fix_time(p):
    # V2.5 has a GPS validity bit and no fix timestamp. Other journal formats may carry one.
    d=p.data
    if d.get("gps_fix_capture_s") is not None:
        t=float(d["gps_fix_capture_s"])
    elif d.get("gps_fix_age_s") is not None:
        t=p.t-float(d["gps_fix_age_s"])
    elif d.get("gps_fix_utc") and p.row.get("receive_utc") and p.row.get("timestamp_source") in ("live_capture_utc","recorded_capture_utc"):
        try:
            fix=datetime.fromisoformat(d["gps_fix_utc"])
            heard=datetime.fromisoformat(p.row["receive_utc"])
            if not fix.tzinfo or not heard.tzinfo:
                return p.t,False
            t=p.t+(fix-heard).total_seconds()
        except (ValueError,TypeError):
            return p.t,False
    else:
        return p.t,False
    return (t,True) if math.isfinite(t) and t<=p.t else (p.t,False)


class PeriodBook:
    def __init__(self, mission):
        self.m=mission
        self.heard=defaultdict(dict)  # Per-ID last observation within each period.
        self.gps=defaultdict(set)
        self.node_periods=defaultdict(list)

    def count(self,ids):
        return sum(node in self.m.roster for node in ids) if self.m.roster else len(ids)

    def hear(self,p):
        i=self.m.index(p.t)
        members=self.heard[i]
        if p.node not in members:
            insort(self.node_periods[p.node],i)
        members[p.node]=max(members.get(p.node,p.t),p.t)
        if valid_fix(p):
            t,dated=fix_time(p)
            if not dated or self.m.index(t)==i:
                self.gps[i].add(p.node)

    def current(self,now,finished):
        return self.m.index(math.nextafter(now,-math.inf) if finished and now>0 else now)

    def info(self,i,now,settled,finished=False,gaps=()):
        start=self.m.start(i)
        end=start+self.m.period
        partial=start < -1e-8 or (finished and end>now+1e-8)
        gap=any(a<min(now,end) and b>max(start,0.) for a,b in gaps)
        final=finished or settled>=end
        heard=self.count(self.heard.get(i,{}))
        state=("Partial period" if partial else "Target met" if heard>=self.m.target else "Below target") if final else (
            "Resolving packets" if now>=end else "In progress")
        return dict(index=i,start_s=start,end_s=end,heard=heard,gps=self.count(self.gps.get(i,())),
                    final=final,partial=partial,gap=gap,met=heard>=self.m.target,state=state)

    def report(self,duration,gaps=()):
        if duration<=0:
            return []
        return [self.info(i,duration,duration,True,gaps)
                for i in range(self.m.index(0),self.current(duration,True)+1)]

    def roster_rows(self,i):
        seen=self.heard.get(i,{})
        ids=sorted(set(self.m.roster)-seen.keys()) if self.m.roster else sorted(seen)
        rows=[]
        for node in ids:
            indices=self.node_periods.get(node,[])
            j=bisect_right(indices,i)-1
            last=self.heard[indices[j]][node] if j>=0 else None
            rows.append((node,last,self.m.roster.get(node,{})))
        return rows


def saved_mission(folder,summary):
    folder=Path(folder)
    path=folder/"mission.json"
    m=Mission.read(json.loads(path.read_text(encoding="utf-8")) if path.exists() else summary.get("mission"))
    scoring=folder/"score.json"
    tx=json.loads(scoring.read_text(encoding="utf-8")).get("tx_to_capture_offset_s") if scoring.exists() else None
    return m.aligned(summary.get("sidecar",{}),tx)
