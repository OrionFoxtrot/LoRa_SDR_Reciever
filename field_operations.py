"""Period coverage, archive inspection, storage health and offline comparison controls."""
import csv
import json
import math
from pathlib import Path
from itertools import islice
import shutil
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from config import monitor_profile_name, profile_from_settings
from front_end import short_name
from fleet_view import Fleet, JournalCursor
from iq_recorder import MIN_FREE_BYTES
from mission import saved_mission
from operator_panels import clock_text, table
from reprocess import capture_for, reprocess, inspect_comparison

IQ_STALL_SECONDS = 1.5


def archive(folder):
    folder=Path(folder).resolve()
    s=json.loads((folder/"summary.json").read_text(encoding="utf-8-sig"))
    if s.get("status") not in ("complete","stopped","failed"):
        raise ValueError("Choose a finished run folder containing summary.json and packets.csv.")
    journal=folder/"packets.csv"
    with journal.open(encoding="utf-8-sig",newline="") as stream:
        fields=csv.DictReader(stream).fieldnames or []
        if not {"capture_s","carrier_hz","sf","payload_hex"}<=set(fields):
            raise ValueError("The saved packet journal is missing required columns.")
    if not isinstance(s.get("packets"),int):
        with journal.open(encoding="utf-8-sig",newline="") as stream:
            s["packets"]=sum(1 for _ in csv.DictReader(stream))
    return folder,s,profile_from_settings(s["profile"]),saved_mission(folder,s)


class FieldOperations:
    def __init__(self,ui):
        self.u=ui
        self.disk_text=""
        self.disk_at=0.
        self.saved_source=tk.StringVar()
        self.reset()

    def reset(self):
        self.summary={}
        self.journal_warning=""
        self.archived=False
        self.label=""
        self.comparison=None
        self.disk_at=0.
        self.iq_clock=None

    def open_dialog(self):
        folder=filedialog.askdirectory(parent=self.u.root,title="Open saved receiver run")
        if folder:
            try:
                self.open_saved(folder)
            except (ValueError,KeyError,OSError,TypeError) as exc:
                messagebox.showerror("Open Saved Run",str(exc),parent=self.u.root)

    def open_saved(self,folder,mission=None,keep_selection=False):
        u=self.u
        if u.worker or u.job:
            raise ValueError("Finish the current receiver operation before opening another run.")
        folder,s,p,m=archive(folder)
        comparison_path=folder/"comparison.json"
        comparison=json.loads(comparison_path.read_text(encoding="utf-8")) if comparison_path.exists() else None
        scoring=folder/"score.json"
        scored=json.loads(scoring.read_text(encoding="utf-8")) if scoring.exists() else None
        selected=u.panels.selected_id if keep_selection else None
        u.close_run_dialogs()
        u.mail=type(u.mail)()
        u.stop=u.terminal=None
        u.release_model(mission or m)
        u.panels.reset()
        u.generic.reset()
        u.panels.selected_id=selected
        self.reset()
        self.archived=True
        self.summary=s
        self.comparison=comparison
        self.label="Replay + SIC" if s.get("sic_enabled") else "Live primary" if s["mode"]=="live" else "Replay primary"
        u.phase="loading"
        u.path=u.open_path=folder
        u.name.set(s.get("run_name") or folder.name)
        self.saved_source.set("Live RTL-SDR" if s["mode"]=="live" else "IQ replay")
        u.source_box.configure(textvariable=self.saved_source)
        u.monitor_box.set(monitor_profile_name(p.carriers_hz))
        u.journal=folder/"packets.csv"
        u.cursor=JournalCursor()
        u.snapshot=dict(committed=s["packets"],capture_s=s.get("capture_seconds",0),settled_s=s.get("capture_seconds",0),
                        gaps=s.get("gap_ranges_s",[]),gain_db=s.get("gain_db",s.get("sidecar",{}).get("gain_db")),
                        dropped_samples=s.get("dropped_samples",0),recorder_warning=(s.get("recording") or {}).get("stopped_reason"),
                        gain_warning=(s.get("gain_setup") or {}).get("warning") or s.get("front_end_gain_reminder"),
                        front_end=s.get("front_end",s.get("sidecar",{}).get("front_end")))
        u.snapshot.update(rf_diagnostics=s.get("rf_diagnostics",{}), branches=s.get("branches",{}),
                          cfo_margin=s.get("cfo_margin",{}),dropout_count=s.get("dropout_count",0))
        u.rf_panel.update_data(u.snapshot)
        if "gap_ranges_s" not in s and s.get("mode")=="replay" and s.get("recorded_unavailable_samples"):
            from sources import Layout
            recording=capture_for(folder,s)
            if recording:
                u.snapshot["gaps"]=Layout.read(recording).gap_ranges(p.sample_rate_hz)
        u.panels.gaps=u.snapshot["gaps"]
        u.panels.configure_channels(p)
        u.generic.configure(p)
        u.state.configure(text="Reading journal")
        fault=(s.get("recording") or {}).get("stopped_reason")
        iq_label=("Synthetic replay" if "synthetic" in s.get("evidence","").lower() else "Recorded replay"
                  if s["mode"]=="replay" else "IQ saved" if capture_for(folder,s) else "IQ not saved")
        u.recording.configure(text="Recording fault" if fault else iq_label,
                              style="Alert.TLabel" if fault else "Field.TLabel")
        u.elapsed.configure(text=clock_text(u.snapshot["capture_s"],True))
        u.prr.grid_remove()
        if scored and scored.get("transmit_log") and scored.get("offered",0)>0:
            u.display_prr(scored)
        u.health_state.configure(text="Journal replay",style="Field.TLabel")
        if fault or s.get("status")=="failed":
            u.health_state.configure(text="Recording fault" if fault else "Run failed",style="Alert.TLabel")
        gain=u.snapshot["gain_db"]
        back=s.get("peak_backlog_s")
        peak=s.get("peak_block_clip_fraction")
        counts=s.get("crc_counts",{})
        for k,v in {"Backlog":f"peak {back:.2f} s" if back is not None else "—",
                    "Tuner gain":f"{gain:g} dB" if gain is not None else "—",
                    "Front end":short_name(u.snapshot["front_end"]),
                    "ADC clipping":f"peak {100*peak:.3f}%" if peak is not None else "—",
                    "Dropped IQ blocks":str(s.get("dropped_blocks") if s.get("dropped_blocks") is not None else "unknown")
                                        + (f"  ·  {s['dropout_count']} dropouts" if "dropout_count" in s else ""),
                    "Recorder drops":(f"{((s['recording']['bytes_dropped']-s['recording'].get('source_gap_bytes',0)+1)//2):,} smp" if
                                      (s.get("recording") or {}).get("bytes_dropped") is not None else "—"),
                    "CRC passes · PHY / App":f"{counts.get('phy_crc_ok/True',0)} / {counts.get('application_crc_ok/True',0)}"}.items():
            u.health[k].configure(text=v,style="Alert.TLabel" if k=="Tuner gain" and u.snapshot["gain_warning"] else "Numeric.TLabel")
        u.detail.configure(text=f"Opening {folder.name}")
        u.footer_pinned=False
        u.controls()

    def loaded(self):
        u=self.u
        u.phase="saved"
        u.state.configure(text="Run saved")
        detail=self.journal_warning or self.summary.get("error") or u.snapshot.get("recorder_warning") or u.snapshot.get("gain_warning")
        u.detail.configure(text=f"{self.label}   ·   {u.path.name}   ·   {u.model.total:,} journal rows"+(f"   ·   {detail}" if detail else ""))
        u.controls()

    def health_tick(self):
        u=self.u
        t=time.monotonic()
        if t>=self.disk_at:
            self.disk_at=t+5
            target=u.path or Path(u.setup.output.get())
            try:
                while not target.exists() and target.parent!=target:
                    target=target.parent
                free=shutil.disk_usage(target).free
                hours=max(0,free-MIN_FREE_BYTES)/2_000_000/3600
                self.disk_text=f"{free/2**30:.1f} GiB free  ·  ~{hours:.1f} h IQ capacity"
            except OSError:
                self.disk_text="Disk capacity unavailable"
        d=u.snapshot
        now=d.get("capture_s",0.)
        live=self.summary.get("mode")=="live" if self.archived else u.mode.get()=="Live RTL-SDR"
        listening_status = "Not listening"
        if live and u.phase=="running":
            last = u.mail.iq
            if last is None:
                last = d.get("last_iq_wall")
            age = None if last is None else max(0., t-last)
            iq = "Waiting for IQ" if age is None else f"Last IQ {age:.1f} s ago"
            if not u.stop.is_set():
                if d.get("reconnecting"):
                    listening_status = "Reconnecting…"
                elif age is None:
                    listening_status = "Waiting for IQ"
                elif age > IQ_STALL_SECONDS:
                    listening_status = "IQ stalled"
                    iq = f"{listening_status}  ·  {iq}"
                else:
                    listening_status = "Listening"
            v=d.get("last_valid_wall")
            valid=f"Last trusted {max(0,t-v):.1f} s ago" if v else "No trusted packet yet"
        else:
            if live and u.journal:
                iq="Capture ended" if now>0 else "No IQ captured"
            else:
                iq="Replay paused" if u.worker and u.replay_pause.is_set() else "Recording time" if u.journal else "SDR idle"
            valid=f"Last trusted {max(0,now-u.model.last_trusted):.1f} s ago" if u.model.last_trusted is not None else "No trusted packet yet"
        u.set_listening_status(listening_status)
        u.field_health.configure(text=f"{iq}    ·    {valid}    ·    {self.disk_text}",
                                 style="Alert.TLabel" if listening_status in ("IQ stalled","Reconnecting…") else "Foot.TLabel")

    def periods_dialog(self):
        u=self.u
        top=tk.Toplevel(u.root)
        top.title("Period coverage and roster")
        top.geometry("680x475")
        box=ttk.Frame(top,padding=12)
        box.pack(fill="both",expand=True)
        book=u.model.book
        now=u.snapshot.get("capture_s",0)
        done=u.phase in ("saved","error")
        settled=u.snapshot.get("settled_s",0) if u.cursor.count>=u.snapshot.get("committed",0) else -math.inf
        intervals=[book.info(i,now,settled,done,u.snapshot.get("gaps",[]))
                   for i in range(book.m.index(0),book.current(now,done)+1)]
        ttk.Label(box,text="Period coverage",style="Heading.TLabel").pack(anchor="w")
        ttk.Label(box,text=f"{book.m.alignment}   ·   {book.m.size} ChipSats   ·   Target {book.m.target}",
                  style="Small.TLabel").pack(anchor="w")
        selector=ttk.Combobox(box,state="readonly",width=72,values=[
            f"Period {v['index']+1}   ·   {v['start_s']:g}–{v['end_s']:g} s   ·   {v['heard']} heard   ·   {v['state']}"+(" · gap" if v["gap"] else "") for v in intervals])
        selector.pack(fill="x",pady=5)
        selector.current(len(intervals)-1)
        label=ttk.Label(box,style="Field.TLabel")
        label.pack(anchor="w")
        frame=ttk.Frame(box)
        frame.pack(fill="both",expand=True,pady=6)
        tree=table(frame,(("id","ChipSat",70),("last","Last heard (s)",105),("fc","Assigned MHz",100),("sf","Assigned SF",85)))
        def select(*_):
            v=intervals[selector.current()]
            details=[f"{v['gps']} reporting valid GPS", "Missing roster IDs" if book.m.roster else "Observed IDs"]
            if not v["final"]:
                details.append("provisional")
            if v["partial"] and v["state"]!="Partial period":
                details.append("partial period")
            if any(a<v["end_s"] and b>v["start_s"] for a,b in self.summary.get("recording_gap_ranges_s") or ()):
                details.append("recording gap")
            if self.summary.get("dropped_samples") and "gap_ranges_s" not in self.summary and not u.snapshot.get("gaps"):
                details.append("gap timing unknown")
            label.configure(text="   ·   ".join(details))
            tree.delete(*tree.get_children())
            for node,last,assignment in book.roster_rows(v["index"]):
                fc=assignment.get("carrier_hz")
                tree.insert("","end",values=(node,f"{last:.3f}" if last is not None else "Never",
                                      f"{int(fc)/1e6:.3f}" if fc else "—",assignment.get("sf") or "—"))
        selector.bind("<<ComboboxSelected>>",select)
        select()
        ttk.Button(box,text="Close",command=top.destroy).pack(anchor="e")

    def start_sic(self):
        u=self.u
        if u.job_kind=="sic":
            u.job_stop.set()
            u.detail.configure(text="Cancelling SIC and finishing analysis writes…")
            return
        if self.comparison:
            self.compare_dialog()
            return
        if not u.path or u.phase!="saved":
            return
        s=json.loads((u.path/"summary.json").read_text(encoding="utf-8"))
        if s.get("sic_enabled"):
            folder=u.path
            mission=u.model.book.m
            u.run_job(lambda:inspect_comparison(folder,folder,mission,"packets_primary.csv"),"compare")
            return
        if capture_for(u.path,s) is None:
            messagebox.showinfo("Reprocess with SIC","IQ was not saved or the original recording is unavailable. The packet journal can still be inspected and exported.",parent=u.root)
            return
        folder=u.path
        mission=u.model.book.m
        def work():
            return reprocess(folder,u.job_stop,lambda d:u.sic_updates.put("progress",d),mission)
        u.detail.configure(text="Reprocessing recorded IQ · starting primary replay…")
        u.run_job(work,"sic")

    def compare_dialog(self):
        u=self.u
        d=self.comparison
        if not d:
            return
        top=tk.Toplevel(u.root)
        top.title("Primary and SIC comparison")
        top.geometry("720x420")
        box=ttk.Frame(top,padding=14)
        box.pack(fill="both",expand=True)
        ttk.Label(box,text="Replay + SIC",style="Heading.TLabel").pack(anchor="w")
        ttk.Label(box,text=f"{d.get('additional_phy_valid',d['additional_valid'])} additional PHY-valid   ·   {d['additional_valid']} trusted   ·   {d['sic_only']} SIC-only   ·   "
                          f"{d['newly_meeting_target']} periods newly at target").pack(anchor="w",pady=5)
        if d.get("original_gap_locations_unknown"):
            ttk.Label(box,text="Original run reported dropped samples with unknown gap times.",style="Stale.TLabel").pack(anchor="w")
        if d.get("original_capture_seconds",0)>d["comparison_seconds"]+.001:
            ttk.Label(box,text=f"Common recorded interval 0–{d['comparison_seconds']:.3f} s",style="Stale.TLabel").pack(anchor="w")
        frame=ttk.Frame(box)
        frame.pack(fill="both",expand=True)
        tree=table(frame,(("p","Period",55),("a","Primary IDs",75),("b","With SIC",72),("extra","Added IDs",110),("status","Coverage",130)))
        for r in d["periods"]:
            text="Partial" if r["partial"] else "Newly met target" if r["newly_met"] else "Target met" if r["with_sic"]>=u.model.book.m.target else "Below target"
            tree.insert("","end",values=(r["period"],r["primary"],r["with_sic"],", ".join(map(str,r["additional_ids"])) or "—",text+(" · gap" if r["gap"] else "")))
        buttons=ttk.Frame(box)
        buttons.pack(fill="x",pady=5)
        ttk.Button(buttons,text="Inspect SIC-only packets",command=lambda:(top.destroy(),self.inspect_sic())).pack(side="left")
        if Path(d["primary_run"])!=u.path:
            ttk.Button(buttons,text="Open primary run",command=lambda:(top.destroy(),self.open_saved(d["primary_run"]))).pack(side="right")

    def inspect_sic(self):
        u=self.u
        top=tk.Toplevel(u.root)
        top.title("SIC-only PHY-valid recoveries")
        top.geometry("940x480")
        box=ttk.Frame(top,padding=(7,6))
        box.pack(fill="both",expand=True)
        tree=table(box,(("t","Capture (s)",80),("id","ChipSat",65),("fc","Carrier (MHz)",90),("sf","SF",36),
                        ("seq","Counter",55),("app","App CRC",65),("raw","Payload (hex)",400)))
        source=Path((self.comparison or {}).get("sic_only_file",u.path/"sic_only.csv"))
        handle=(source if source.exists() else u.journal).open(encoding="utf-8-sig",newline="")
        rows=(r for r in csv.DictReader(handle) if r["pass_name"]=="sic" and r["phy_crc_ok"]=="True")
        top.bind("<Destroy>",lambda event:handle.close() if event.widget==top else None,add="+")
        actions=ttk.Frame(top,padding=4)
        actions.pack(fill="x")
        info=ttk.Label(actions,style="Small.TLabel")
        info.pack(side="left",padx=5)
        seen=0
        def load():
            nonlocal seen
            from operator_panels import crc_text
            tree.delete(*tree.get_children())
            page=list(islice(rows,250))
            for r in page:
                tree.insert("","end",values=(r["capture_s"],r["chipsat_id"] or "—",int(r["carrier_hz"])/1e6,
                    r["sf"],r["packet_counter"] or "—",crc_text(r["application_crc_ok"]),r["payload_hex"]))
            seen+=len(page)
            info.configure(text=f"Rows {seen-len(page)+1}–{seen}" if page else "No further recoveries")
            if len(page)<250:
                next_page.state(["disabled"])
        next_page=ttk.Button(actions,text="Next 250",command=load)
        next_page.pack(side="left")
        def copy():
            chosen=tree.selection()
            if chosen:
                top.clipboard_clear()
                top.clipboard_append(tree.item(chosen[0],"values")[-1])
        ttk.Button(actions,text="Copy payload",command=copy).pack(side="left",padx=4)
        ttk.Button(actions,text="Export SIC-only CSV",command=lambda:u.choose_export("sic")).pack(side="right")
        load()
