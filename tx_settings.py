"""Compare an operator's per-radio settings export to the intended receiver profile."""
import csv


def boolean(s):
    return {"true":True,"false":False,"1":True,"0":False,"on":True,"off":False}[s.lower().strip()]


def check(p, path):
    branches=set()
    errors=[]
    with path.open(encoding="utf-8-sig",newline="") as f:
        for i,row in enumerate(csv.DictReader(f),2):
            try:
                sf,fc=int(row["sf"]),int(row["carrier_hz"])
                actual=dict(sync_word=int(row["sync_word"],0), bandwidth_hz=int(row["bandwidth_hz"]),
                            explicit_header=boolean(row["explicit_header"]), phy_crc=boolean(row["phy_crc"]),
                            cr_denominator=int(row["cr_denominator"]),preamble_symbols=int(row["preamble_symbols"]),
                            invert_iq=boolean(row["invert_iq"]),ldro=boolean(row["ldro"]))
            except (KeyError,TypeError,ValueError) as exc:
                raise ValueError(f"Invalid transmitter readback at row {i}: {exc}") from exc
            branches.add((fc,sf))
            expected=dict(sync_word=p.sync_word,bandwidth_hz=p.bandwidth_hz,explicit_header=p.explicit_header,
                          phy_crc=p.phy_crc,cr_denominator=p.coding_rate_denominator,
                          preamble_symbols=p.preamble_symbols,invert_iq=p.invert_iq,ldro=p.de(sf))
            if fc not in p.carriers_hz or sf not in p.spreading_factors:
                errors.append(dict(row=i,carrier_hz=fc,sf=sf,reason="unconfigured branch"))
            for key,val in expected.items():
                if actual[key]!=val:
                    errors.append(dict(row=i,field=key,actual=actual[key],expected=val))
    missing=sorted({(fc,sf) for fc in p.fleet_carriers_hz for sf in p.spreading_factors}-branches)
    return dict(confirmed=not missing and not errors, missing_branches=missing,disagreements=errors)
