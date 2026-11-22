"""Command-line entry points for the DeSCENT LoRa receiver."""
import argparse
from dataclasses import replace
import importlib.metadata
import json
from pathlib import Path
import signal
import sys
import threading
from config import (ROOT, DEFAULT_PROFILE, MONITOR_CHOICES, MONITOR_PROFILES, EventIQLimits,
                    carrier_preset, read_profile, search_half_width)
from front_end import FrontEndPreferences, from_fields, preset, preset_for


def setting_args(sp):
    sp.add_argument("--profile",type=Path)
    sp.add_argument("--preset",choices=MONITOR_CHOICES,help="carrier preset, or Custom with --carriers")
    sp.add_argument("--center",type=int,help="tuner center Hz; a replay sidecar supplies the actual center")
    sp.add_argument("--carriers",type=int,nargs="+",help="absolute carrier frequencies in Hz")
    sp.add_argument("--sfs",type=int,nargs="+")
    sp.add_argument("--roster",type=Path,help="CSV with chipsat_id and optional carrier_hz and sf")
    sp.add_argument("--bw",type=int,help="occupied bandwidth in Hz")
    sp.add_argument("--max-cfo-hz",type=search_half_width,metavar="HZ",
                    help="frequency search half-width, 10000–30000 Hz (default profile 18000)")
    sp.add_argument("--out",type=Path,default=ROOT/"runs",help="parent for a new unique run directory")
    sp.add_argument("--quiet",action="store_true")
    limits = EventIQLimits()
    sp.add_argument("--event-iq-per-kind-hour", type=int, default=limits.per_kind_hour,
                    help="maximum clips per trigger kind per rolling capture hour (default 6)")
    sp.add_argument("--event-iq-max-mb", type=int, default=limits.max_mb,
                    help="total decimal MB for event clips and their sidecars (default 1000)")


def printer(data):
    counts = " ".join(f"{k}={v['phy_pass']}/{v['packets']}" for k,v in data["branches"].items())
    print(f"{data['capture_s']:8.3f}s  {counts}  IDs={data['recent_ids']}  rails={100*data['clipped_fraction']:.3f}%  drops={data['dropped_samples']}  recorder_drops={data['recorder_dropped_samples']}  backlog={data['backlog_s']:.3f}s  {data['recorder_warning'] or ''}",flush=True)


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    cmds=ap.add_subparsers(dest="command",required=True)
    cmds.add_parser("doctor",help="check decoder, GUI and rtl_tcp installation")
    ui=cmds.add_parser("gui")
    ui.add_argument("--bench",action="store_true",help="two-radio coverage preset (target 2, 30 seconds)")
    ui.add_argument("--open-run",type=Path,metavar="FOLDER",help="inspect a completed journal without decoding IQ")
    ui.add_argument("--replay",nargs="?",const="",metavar="IQ_FILE",help="open the replay GUI with an optional recording")
    sp=cmds.add_parser("live")
    setting_args(sp)
    sp.add_argument("--gain",type=float,default=10.)
    sp.add_argument("--ppm",type=int,default=0)
    sp.add_argument("--device",type=int,default=0)
    sp.add_argument("--auto-gain",action="store_true")
    sp.add_argument("--connect",help="use an existing rtl_tcp HOST:PORT")
    sp.add_argument("--seconds",type=float,help="capture duration after tuning and gain setup")
    sp.add_argument("--no-record",action="store_true")
    sp.add_argument("--front-end", choices=("lana","lana-wb","none"), help="front-end hardware selection")
    sp.add_argument("--lna-model", help="use None for a direct SDR arm")
    sp.add_argument("--lna-gain-db", type=float)
    sp.add_argument("--lna-power", help="descriptive metadata")
    sp.add_argument("--rf-filter")
    sp.add_argument("--usb-supply", help="e.g. laptop or battery bank")
    rp=cmds.add_parser("replay")
    setting_args(rp)
    rp.add_argument("iq",type=Path)
    rp.add_argument("--sic",action="store_true")
    rp.add_argument("--paced",action="store_true")
    sc=cmds.add_parser("score")
    sc.add_argument("run",type=Path)
    sc.add_argument("tx_csv",type=Path)
    sc.add_argument("--offset-s",type=float,default=0.)
    sc.add_argument("--tolerance-s",type=float,default=.05)
    ck=cmds.add_parser("check-tx",help="compare transmitter settings read back on the bench with the profile")
    ck.add_argument("readback_csv",type=Path)
    ck.add_argument("--profile",type=Path,default=DEFAULT_PROFILE)
    args=ap.parse_args(argv)
    if args.command=="gui":
        from operator_gui import open_gui
        open_gui("Replay" if args.replay is not None else "Live",args.replay or "",args.open_run,bench=args.bench)
        return 0
    if args.command=="doctor":
        import tkinter
        from phy import Codec
        from sources import find_rtl
        p=read_profile()
        for sf in p.spreading_factors:
            Codec(p,p.carriers_hz[0],sf)
        version=importlib.metadata.version("lora-phy")
        if version!="0.3.0":
            raise ValueError("Use the tested lora-phy==0.3.0. Run setup.bat.")
        print(f"Python {sys.version.split()[0]}   pyLoRaPHY {version}   Tcl/Tk {tkinter.TkVersion}")
        print("rtl_tcp:",find_rtl() or "missing (offline replay available)")
        print(f"Capture covers {len(p.carriers_hz)*len(p.spreading_factors)} default carrier/SF branches.")
        return 0
    if args.command=="check-tx":
        from tx_settings import check
        result=check(read_profile(args.profile),args.readback_csv)
        print(json.dumps(result,indent=2))
        return 0 if result["confirmed"] else 1
    if args.command=="score":
        from reports import score
        print(json.dumps(score(args.run,args.tx_csv,args.offset_s,args.tolerance_s),indent=2))
        return 0
    from run import live,replay
    from mission import Mission, read_roster
    from sources import Layout
    def plan_warning(messages):
        for msg in messages:
            print(f"Carrier plan warning: {msg}", file=sys.stderr, flush=True)
    def rf_warning(msg):
        print(msg, file=sys.stderr, flush=True)
    mission = None
    if args.roster:
        meta = Layout.read(args.iq).metadata if args.command == "replay" else {}
        mission = replace(Mission.read(meta.get("mission")), roster=read_roster(args.roster))
    preferences = FrontEndPreferences()
    p=read_profile(args.profile or DEFAULT_PROFILE)
    if args.profile is None:
        recorded = Layout.read(args.iq).recorded_profile(args.iq) if args.command=="replay" else None
        p = recorded or preferences.carrier_profile(p)
    if args.preset in MONITOR_PROFILES:
        p = carrier_preset(p,args.preset)
        if (args.center is not None and args.center != p.center_hz or
                args.carriers is not None and tuple(args.carriers) != p.carriers_hz):
            ap.error("Choose Custom to override a named preset's carriers or tuner center.")
    overrides={k:v for k,v in (("center_hz",args.center),("carriers_hz",args.carriers),
                               ("spreading_factors",args.sfs),("bandwidth_hz",args.bw),
                               ("max_cfo_hz",args.max_cfo_hz)) if v is not None}
    p=replace(p,**overrides)
    p=replace(p,carriers_hz=tuple(p.carriers_hz),
              monitor_only_hz=tuple(fc for fc in p.monitor_only_hz if fc in p.carriers_hz)).checked()
    event_limits = EventIQLimits(args.event_iq_per_kind_hour, args.event_iq_max_mb)
    stop=threading.Event()
    signal.signal(signal.SIGINT,lambda *_:stop.set())
    if args.command=="live":
        choice,front = (args.front_end,preset(args.front_end)) if args.front_end else preferences.selected()
        overrides = dict(lna_model=args.lna_model,nominal_gain_db=args.lna_gain_db,power_source=args.lna_power,
                         filter=args.rf_filter,usb_supply=args.usb_supply)
        metadata = from_fields({**front, **{k:v for k,v in overrides.items() if v is not None}})
        folder=live(p,args.out,args.gain,args.ppm,args.device,args.seconds,args.connect,
                    not args.no_record,args.auto_gain,stop,None if args.quiet else printer,
                    mission=mission, plan_warning=plan_warning, rf_warning=rf_warning, event_limits=event_limits,
                    front_end=metadata, front_choice=choice if metadata==front else preset_for(metadata),
                    front_preferences=preferences)
    else:
        folder=replay(p,args.iq,args.out,sic=args.sic,paced=args.paced,stop=stop,
                      notify=None if args.quiet else printer, mission=mission, plan_warning=plan_warning,
                      rf_warning=rf_warning, event_limits=event_limits)
    if args.preset or args.carriers or args.command=="live":
        preferences.save_carriers(p,args.preset)
    print(folder)
    return 0


if __name__=="__main__":
    try:
        sys.exit(main())
    except (ValueError,OSError,RuntimeError,EOFError) as exc:
        print(f"Receiver error: {exc}",file=sys.stderr)
        sys.exit(1)
