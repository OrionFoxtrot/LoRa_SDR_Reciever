"""Disposable on-disk index of trusted journal observations for interactive field plots."""
import math
import sqlite3
import weakref
from mission import fix_time, valid_fix

SERIES={
    "Preamble power":("power","dBFS"), "GPS altitude · MSL":("gps","m MSL"),
    "Barometric altitude":("baro","m barometric"), "Both altitudes":("both","m"),
    "Battery SoC":("soc","%"), "Temperature":("temp","°C"), "Pressure":("pressure","hPa"),
    "Carrier offset":("cfo","Hz"),
}


def number(v):
    if v in ("",None):
        return None
    x=float(v)
    return x if math.isfinite(x) else None


def east_north(origin,lat,lon):
    # WGS84 geodetic surface -> ECEF -> local tangent plane (EPSG method 9837).
    # MSL is not ellipsoidal height. Project the ground footprint at ellipsoid height zero.
    def surface(a,b):
        a,b=math.radians(a),math.radians(b)
        e2=0.0066943799901413165
        n=6378137/math.sqrt(1-e2*math.sin(a)**2)
        return n*math.cos(a)*math.cos(b),n*math.cos(a)*math.sin(b),n*(1-e2)*math.sin(a)
    a,b=map(math.radians,origin)
    dx,dy,dz=(v-u for v,u in zip(surface(lat,lon),surface(*origin)))
    return -math.sin(b)*dx+math.cos(b)*dy, -math.sin(a)*(math.cos(b)*dx+math.sin(b)*dy)+math.cos(a)*dz


class Observations:
    def __init__(self):
        self.db=sqlite3.connect("",check_same_thread=False)  # Private auto-deleted temp database.
        self.db.row_factory=sqlite3.Row
        self.db.executescript("""
            PRAGMA cache_size=-1024;
            PRAGMA journal_mode=OFF;
            CREATE TABLE points(n INTEGER,t REAL,seq INTEGER,fc INTEGER,sf INTEGER,pass TEXT,
              power REAL,gps REAL,baro REAL,soc REAL,temp REAL,pressure REAL,cfo REAL,
              lat REAL,lon REAL,fix_t REAL,dated INTEGER,raw TEXT,utc TEXT);
            CREATE INDEX by_node_time ON points(n,t);
            CREATE INDEX fixes_by_time ON points(n,t) WHERE lat IS NOT NULL;
        """)
        self.cleanup=weakref.finalize(self,self.db.close)

    def add(self,p):
        d=p.data
        flags=d.get("valid",{})
        gps=valid_fix(p)
        env=flags.get("environmental")
        t,dated=fix_time(p) if gps else (None,False)
        self.db.execute("INSERT INTO points VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(
            p.node,p.t,p.row.get("packet_counter"),p.fc,p.sf,p.row.get("pass_name") or "primary",
            number(p.row.get("preamble_power_dbfs")),d.get("gps_altitude_msl_m") if gps else None,
            d.get("environmental_altitude_m") if env else None,d.get("state_of_charge_percent") if flags.get("state_of_charge") else None,
            d.get("temperature_C") if env else None,d.get("pressure_hPa") if env else None,number(p.row.get("cfo_hz")),
            d.get("latitude_deg") if gps else None,d.get("longitude_deg") if gps else None,t,dated,
            p.row.get("payload_hex",""),p.row.get("receive_utc","")))

    def lost_fix_between(self,node,a,b):
        return self.db.execute("SELECT 1 FROM points WHERE n=? AND t>? AND t<? AND lat IS NULL LIMIT 1",(node,a,b)).fetchone() is not None

    def series(self,node,lo,hi,fixes=False,limit=450):
        where="n=? AND t BETWEEN ? AND ?"+(" AND lat IS NOT NULL" if fixes else "")
        sql=f"SELECT rowid,* FROM points WHERE {where}"
        rows=self.db.execute(sql+" ORDER BY t,rowid LIMIT ?",(node,lo,hi,limit+1)).fetchall()
        if len(rows)<=limit:
            return rows,False
        # Indexed seeks bound Tk work on multi-day runs. Every point is a real observation.
        first=rows[0]
        last=self.db.execute(sql+" ORDER BY t DESC,rowid DESC LIMIT 1",(node,lo,hi)).fetchone()
        out={first["rowid"]:first,last["rowid"]:last}
        for j in range(1,limit-1):
            at=first["t"]+(last["t"]-first["t"])*j/(limit-1)
            r=self.db.execute(sql+" ORDER BY t,rowid LIMIT 1",(node,at,hi)).fetchone()
            if r:
                out[r["rowid"]]=r
        return sorted(out.values(),key=lambda r:(r["t"],r["rowid"])),True

    def close(self):
        self.cleanup()
