"""Windows fleet operator. Tk reads committed packets in batches from a run-owned journal."""
import csv
import json
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from config import MONITOR_CHOICES, ROOT, describe_front_end
from front_end import gain_reminder, short_name
from fleet_view import Fleet, Filters, JournalCursor, export_csv
from field_operations import FieldOperations
from generic_view import GenericView
from mission import Mission, PeriodBook
from gain import CLIP_FRACTION, manual_gain_db
from operator_panels import DASH, FleetPanels, clock_text
from operator_theme import PAPER, configure
from receiver_setup import ReceiverSetup
from rf_panel import RFPanel
from reports import directory, score, write_json
from run import live, replay


class Mailbox:
    """Each run owns one mailbox. Only the newest health snapshot needs screen time."""
    def __init__(self):
        self.lock=threading.Lock()
        self.progress=self.gain=self.end=None
        self.iq=None
        self.plan_warnings=queue.SimpleQueue()

    def put(self,kind,value):
        with self.lock:
            setattr(self,kind,value)

    def take(self):
        with self.lock:
            messages=(self.gain,self.progress,self.end)
            self.gain=self.progress=self.end=None
        return messages


class ReceiverWindow:
    def __init__(self,root,initial_mode="Live",initial_iq="",*,front_preferences=None):
        self.root=root
        self.family,self.mono=configure(root)
        root.title("DeSCENT · LoRa fleet receiver")
        root.configure(background=PAPER)
        root.minsize(980,630)
        root.geometry(f"{min(1390,root.winfo_screenwidth()-22)}x{min(825,root.winfo_screenheight()-76)}")
        root.columnconfigure(0,weight=1)
        root.rowconfigure(2,weight=1)
        self.mode=tk.StringVar(value="IQ replay" if initial_mode in ("IQ replay","Replay") else "Live RTL-SDR")
        self.name=tk.StringVar(value="Bench run")
        self.job_kind=None
        self.job_stop=threading.Event()
        self.sic_updates=Mailbox()
        self.replay_pause=threading.Event()
        self.path=self.worker=self.job=self.stop=self.terminal=None
        self.mail=Mailbox()
        self.jobs=queue.SimpleQueue()
        self.snapshot={}
        self.journal=None
        self.cursor=JournalCursor()
        self.model=Fleet()
        self.phase="ready"
        self.pending_new=self.closing=False
        self.footer_pinned=False
        self.run_number=1
        self.open_path=None
        self.setup=None
        self.field=FieldOperations(self)
        self._header()
        bar=self._bar()
        self.views=ttk.Notebook(root)
        self.views.grid(row=2,column=0,sticky="nsew",padx=10,pady=(3,4))
        body=ttk.Frame(self.views,style="Page.TFrame",padding=(2,3,2,2))
        self.views.add(body,text="Fleet")
        body.columnconfigure(0,weight=64,uniform="work")
        body.columnconfigure(1,weight=36,uniform="work")
        body.rowconfigure(0,weight=1)
        log_page=ttk.Frame(self.views)
        self.panels=FleetPanels(body,self.mono,self.score_dialog,log_page)
        self.generic_page=ttk.Frame(self.views,padding=(12,8))
        self.views.add(self.generic_page,text="Generic LoRa")
        self.views.add(log_page,text="Packet log")
        self.rf_panel=RFPanel(self.views)
        self.views.add(self.rf_panel, text="RF diagnostics")
        self.generic=GenericView(self.generic_page,self.panels,lambda:self.choose_export("generic"),self.mono)
        def generic_filter(*_):
            if self.views.index("current")==1:
                self.generic.render(self.model)
        self.panels.fc_filter.trace_add("write",generic_filter)
        self.panels.sf_filter.trace_add("write",generic_filter)
        self.views.bind("<<NotebookTabChanged>>",lambda _:self.generic.render(self.model) if self.views.index("current")==1 else None)
        self._health()
        self.setup=ReceiverSetup(root,ROOT/"runs",self.mode,initial_iq,self.mono,self.settings_changed,front_preferences)
        self._radio_bar(bar)
        self.mode.trace_add("write",self.setup.source_changed)
        self.setup.gain.trace_add("write",self.live_settings_changed)
        self.settings_changed()
        self.panels.render(self.model,0)
        root.protocol("WM_DELETE_WINDOW",self.close)
        self.timer=root.after(100,self.poll)

    def _header(self):
        h=ttk.Frame(self.root,style="Header.TFrame",padding=(14,6))
        h.grid(row=0,column=0,sticky="ew")
        def metric(col, title):
            value = ttk.Label(h, text="0", style="FleetCount.TLabel", anchor="e")
            value.grid(row=0, column=col)
            ttk.Label(h, text=title, style="FleetCaption.TLabel").grid(
                row=0, column=col+1, sticky="w", padx=(7, 22))
            return value

        self.heard_count = metric(0, "ChipSats heard")
        self.valid_count = metric(2, "ChipSats reporting valid data")
        self.packet_count = metric(4, "Packets heard")
        self.failed_count = metric(6, "Failed CRC")
        h.columnconfigure(8, weight=1)
        self.listening_light = ttk.Label(h)
        self.listening_light.grid(row=0, column=8, sticky="e")
        self.set_listening_status("Not listening")

    def set_listening_status(self, status):
        text = f"●  {status}"
        if self.listening_light.cget("text") != text:
            style = "Listening.FleetCaption.TLabel" if status == "Listening" else "Idle.FleetCaption.TLabel"
            self.listening_light.configure(text=text, style=style)

    def update_fleet_counts(self):
        self.heard_count.configure(text=str(len(self.model.nodes)))
        self.valid_count.configure(text=str(self.model.reporting_valid_data))
        self.packet_count.configure(text=f"{self.model.total:,}")
        self.failed_count.configure(text=f"{self.model.failed:,}",
                                   style="Fault.FleetCount.TLabel" if self.model.failed else "FleetCount.TLabel")

    def _bar(self):
        bar=ttk.Frame(self.root,padding=(15,6,15,8))
        bar.grid(row=1,column=0,sticky="ew")
        bar.columnconfigure(3,weight=1)
        for c,title in ((0,"Run name"),(1,"Source")):
            ttk.Label(bar,text=title,style="Small.TLabel").grid(row=0,column=c,sticky="w")
        status=ttk.Frame(bar)
        status.grid(row=0,column=2,columnspan=5,sticky="e")
        self.run_mode=ttk.Label(status,text="Run",style="Small.TLabel")
        self.state=ttk.Label(status,text="Ready",style="Field.TLabel")
        self.recording=ttk.Label(status,style="Field.TLabel")
        self.elapsed=ttk.Label(status,text="00:00.0",style="Numeric.TLabel")
        for col,label,value in ((0,self.run_mode,self.state),
                                (2,ttk.Label(status,text="IQ",style="Small.TLabel"),self.recording),
                                (4,ttk.Label(status,text="Elapsed",style="Small.TLabel"),self.elapsed)):
            label.grid(row=0,column=col,padx=(16,4))
            value.grid(row=0,column=col+1)
        self.name_input=ttk.Entry(bar,textvariable=self.name,width=24)
        self.name_input.grid(row=1,column=0,sticky="ew",padx=(0,9))
        self.source_box=ttk.Combobox(bar,textvariable=self.mode,values=("Live RTL-SDR","IQ replay"),
                                     width=13,state="readonly")
        self.source_box.grid(row=1,column=1,padx=(0,8))
        self.setup_button=ttk.Button(bar,text="Receiver Setup",command=self.show_setup)
        self.setup_button.grid(row=1,column=2,sticky="w")
        more=ttk.Frame(bar)
        more.grid(row=1,column=3,sticky="w",padx=4)
        self.saved_button=ttk.Button(more,text="Open Saved Run",command=self.field.open_dialog)
        self.saved_button.pack(side="left")
        self.pause_button=ttk.Button(more,text="Pause replay",command=self.toggle_pause)
        self.pause_button.pack(side="left",padx=3)
        self.start_button=ttk.Button(bar,text="Start receiver",command=self.start_stop,style="Start.TButton")
        self.start_button.grid(row=1,column=4,padx=(6,0))
        self.new_button=ttk.Button(bar,text="New Run",command=self.new_run)
        self.new_button.grid(row=1,column=5,padx=6)
        self.export_button=ttk.Button(bar,text="Export CSV",command=self.export_dialog)
        self.export_button.grid(row=1,column=6)
        return bar

    def _radio_bar(self, bar):
        radio = ttk.Frame(bar)
        radio.grid(row=2, column=0, columnspan=7, sticky="ew", pady=(5, 0))
        ttk.Label(radio, text="Monitoring profile", style="Field.TLabel").pack(side="left", padx=(0, 6))
        self.monitor_box = ttk.Combobox(radio, values=MONITOR_CHOICES, width=20, state="readonly")
        self.monitor_box.pack(side="left")
        def select(_):
            name = self.monitor_box.get()
            self.setup.apply_monitor_profile(name)
            if name == "Custom":
                self.show_setup()
        self.monitor_box.bind("<<ComboboxSelected>>",select)
        self.live_controls = ttk.Frame(radio)
        self.live_controls.pack(side="left", padx=(18, 0))
        self.record_check = ttk.Checkbutton(self.live_controls, text="Record IQ", variable=self.setup.record,
                                           command=self.live_settings_changed)
        self.record_check.pack(side="left", padx=(0, 14))
        self.auto_check = ttk.Checkbutton(self.live_controls, text="Auto-set gain on start", variable=self.setup.auto,
                                         command=self.live_settings_changed)
        self.auto_check.pack(side="left", padx=(0, 14))
        ttk.Label(self.live_controls, text="Manual gain (dB)", style="Field.TLabel").pack(side="left", padx=(0, 5))
        self.gain_input = ttk.Entry(self.live_controls, textvariable=self.setup.gain, width=6)
        self.gain_input.pack(side="left")
        self.gain_notice = ttk.Label(bar,style="Alert.TLabel",wraplength=900)
        self.gain_notice.grid(row=3,column=0,columnspan=7,sticky="w")
        self.gain_notice.grid_remove()

    def _health(self):
        h=ttk.Frame(self.root,style="Card.TFrame",padding=(12,5))
        h.grid(row=3,column=0,sticky="ew",padx=12)
        self.health_state=ttk.Label(h,text="Ready",style="Field.TLabel")
        self.health_state.grid(row=1,column=0,sticky="w")
        self.health={}
        for c,title in enumerate(("Receiver health","Backlog","Dropped IQ blocks","ADC clipping","Recorder drops",
                                  "Tuner gain","Front end","CRC passes · PHY / App")):
            h.columnconfigure(c,weight=1)
            ttk.Label(h,text=title,style="Small.TLabel").grid(row=0,column=c,sticky="w",padx=(0,6))
            if c:
                label=ttk.Label(h,text=DASH,style="Numeric.TLabel")
                label.grid(row=1,column=c,sticky="w",padx=(0,6))
                self.health[title]=label
        self.prr=ttk.Label(h,style="Field.TLabel")
        foot=ttk.Frame(self.root,style="Page.TFrame",padding=(14,3))
        foot.grid(row=4,column=0,sticky="ew")
        foot.columnconfigure(0,weight=1)
        self.detail=ttk.Label(foot,text="",style="Foot.TLabel")
        self.detail.grid(row=0,column=0,sticky="w")
        self.folder_button=ttk.Button(foot,text="Open Folder",command=self.open_folder)
        self.folder_button.grid(row=0,column=1)
        self.run_folder_button=ttk.Button(foot,text="Open Run Folder",command=lambda:self.open_folder(self.path))
        self.run_folder_button.grid(row=0,column=2,padx=(4,0))
        self.period_button=ttk.Button(foot,text="Periods / roster",command=self.field.periods_dialog)
        self.period_button.grid(row=0,column=3,padx=4)
        self.sic_button=ttk.Button(foot,text="Reprocess with SIC",command=self.field.start_sic)
        self.sic_button.grid(row=0,column=4)
        self.field_health=ttk.Label(h,style="Foot.TLabel")
        self.field_health.grid(row=2,column=0,columnspan=8,sticky="w",pady=(3,0))

    def settings_changed(self):
        if self.setup is None or self.phase!="ready":
            return
        try:
            p=self.setup.selected()
        except ValueError:
            p=None
        self.monitor_box.set(self.setup.carrier_name.get())
        self.panels.configure_channels(p)
        self.generic.configure(p)
        try:
            self.model.book=PeriodBook(self.setup.mission())
        except (ValueError,OSError,TypeError):
            pass
        self.live_settings_changed()
        if self.path is None:
            self.detail.configure(text="Choose a source and start a run.")
        if p:
            self.panels.render(self.model,0)

    def live_settings_changed(self, *_):
        if self.phase != "ready":
            return
        live = self.mode.get() == "Live RTL-SDR"
        self.recording.configure(text=("Armed" if self.setup.record.get() else "Off") if live else "Playback",
                                 style="Field.TLabel")
        gain = self.setup.gain.get().strip()
        gain_text = "Auto" if self.setup.auto.get() else f"{gain} dB" if gain else DASH
        self.health["Tuner gain"].configure(text=gain_text if live else DASH)
        try:
            front = self.setup.front_end() if live else self.setup.recorded_front_end
        except ValueError:
            front = None
        self.health["Front end"].configure(text=short_name(front))
        reminder = (gain_reminder(self.setup.preferences.last_run(self.setup.output.get()),front,False)
                    if live and not self.setup.auto.get() else None)
        if reminder:
            self.gain_notice.configure(text=reminder)
            self.gain_notice.grid()
        else:
            self.gain_notice.grid_remove()
        self.controls()

    def controls(self):
        self.run_mode.configure(text=self.field.label or "Run")
        ready=self.phase=="ready" and not self.pending_new
        if not ready:
            self.gain_notice.grid_remove()
        busy=self.worker is not None
        self.name_input.state(["!disabled"] if ready else ["disabled"])
        self.source_box.configure(state="readonly" if ready else "disabled")
        self.monitor_box.configure(state="readonly" if ready else "disabled")
        live_source = self.mode.get() == "Live RTL-SDR" and not self.field.archived
        if live_source and not self.live_controls.winfo_manager():
            self.live_controls.pack(side="left", padx=(18, 0))
        elif not live_source and self.live_controls.winfo_manager():
            self.live_controls.pack_forget()
        editable = ready and live_source
        self.record_check.state(["!disabled"] if editable else ["disabled"])
        self.auto_check.state(["!disabled"] if editable else ["disabled"])
        self.gain_input.state(["!disabled"] if editable and not self.setup.auto.get() else ["disabled"])
        self.setup_button.state(["disabled"] if self.pending_new else ["!disabled"])
        self.start_button.configure(text="Stop receiver" if busy else "Start receiver")
        self.start_button.state(["!disabled"] if ready or (busy and not self.stop.is_set()) else ["disabled"])
        self.new_button.state(["disabled"] if self.pending_new else ["!disabled"])
        available=bool(self.path and self.journal and self.job is None and not self.pending_new)
        self.export_button.state(["!disabled"] if available else ["disabled"])
        self.folder_button.state(["!disabled"] if self.open_path else ["disabled"])
        self.panels.score_button.state(["!disabled"] if available and self.phase=="saved" else ["disabled"])
        self.generic.export_button.state(["!disabled"] if available else ["disabled"])
        self.saved_button.state(["disabled"] if busy or self.job or self.pending_new else ["!disabled"])
        self.run_folder_button.state(["!disabled"] if self.path else ["disabled"])
        self.folder_button.grid() if self.open_path and self.open_path!=self.path else self.folder_button.grid_remove()
        self.pause_button.pack(side="left",padx=3) if busy and self.mode.get()=="IQ replay" else self.pause_button.pack_forget()
        self.sic_button.configure(text="Cancel SIC" if self.job_kind=="sic" else "SIC comparison" if self.field.comparison else "Reprocess with SIC")
        self.sic_button.state(["!disabled"] if self.job_kind=="sic" or (self.phase=="saved" and not self.job) else ["disabled"])

    def reserve(self):
        parent=Path(self.setup.output.get()).expanduser().resolve()
        if self.path is None or self.path.parent!=parent:
            self.path=directory(parent)
            write_json(self.path/"summary.json",dict(run_id=self.path.name,run_name=self.name.get(),status="ready"))
        self.open_path=self.path

    def start_stop(self):
        self.finish() if self.worker else self.start()

    def show_setup(self):
        if self.field.archived:
            p=self.field.summary["profile"]
            messagebox.showinfo("Saved receiver setup",f"{self.field.label}\n{p['sample_rate_hz']/1e6:g} MS/s   ·   "
                f"{p['bandwidth_hz']/1000:g} kHz   ·   sync {int(p['sync_word']):#04x}\nCarriers (MHz): "
                +", ".join(str(fc/1e6) for fc in p["carriers_hz"])+"\nSF: "+", ".join(map(str,p["spreading_factors"]))
                +"\n"+describe_front_end(self.snapshot.get("front_end")),parent=self.root)
            return
        self.setup.show(locked=self.phase!="ready",gain=self.snapshot.get("gain_db"))

    def toggle_pause(self):
        if self.replay_pause.is_set():
            self.replay_pause.clear()
            self.pause_button.configure(text="Pause replay")
        else:
            self.replay_pause.set()
            self.pause_button.configure(text="Resume replay")
            self.state.configure(text="Paused")

    def release_model(self,mission=None):
        self.model.close()
        self.model=Fleet(mission)
        self.update_fleet_counts()

    def close_run_dialogs(self):
        def visit(parent):
            for child in parent.winfo_children():
                if isinstance(child,tk.Toplevel):
                    if child!=self.setup.win:
                        child.destroy()
                else:
                    visit(child)
        visit(self.root)

    def start(self):
        if self.phase!="ready" or self.worker or self.pending_new:
            return
        is_live=self.mode.get()=="Live RTL-SDR"
        try:
            p=self.setup.selected()
            mission=self.setup.mission()
            if is_live:
                args=dict(gain=10. if self.setup.auto.get() else manual_gain_db(self.setup.gain.get()),
                          ppm=int(self.setup.ppm.get()),auto_gain=self.setup.auto.get(),record=self.setup.record.get(),
                          front_end=self.setup.front_end(),front_choice=self.setup.front_choice(),
                          front_preferences=self.setup.preferences)
            else:
                if not Path(self.setup.capture.get()).is_file():
                    picked=filedialog.askopenfilename(parent=self.root,title="Choose an IQ recording",
                                                      filetypes=[("Unsigned 8-bit IQ","*.iq"),("All files","*.*")])
                    if not picked:
                        return
                    self.setup.capture.set(picked)
                    p=self.setup.selected()
                args=dict(sic=self.setup.sic.get(),paced=self.setup.paced.get(),pause=self.replay_pause)
            args["event_limits"] = self.setup.event_limits()
            self.setup.save_carriers()
            self.reserve()
        except (ValueError,OSError) as exc:
            messagebox.showerror("Receiver settings",str(exc),parent=self.root)
            return
        self.setup.hide()
        self.field.label="Live primary" if is_live else "Replay + SIC" if self.setup.sic.get() else "Replay primary"
        self.model.book=PeriodBook(mission)
        folder=self.path
        iq=self.setup.capture.get()
        mail=self.mail
        self.stop=threading.Event()
        args.update(stop=self.stop,notify=lambda d:mail.put("progress",d),folder=folder,mission=mission,
                    plan_warning=mail.plan_warnings.put,
                    name=self.name.get().strip() or folder.name)
        if is_live:
            args["gain_progress"]=lambda msg:mail.put("gain",msg)
            args["iq_heartbeat"]=lambda stamp:mail.put("iq",stamp)
        self.phase="running"
        self.state.configure(text="Starting")
        self.detail.configure(text="Opening RTL-SDR…" if is_live else f"Opening {Path(iq).name}…")
        def receive():
            try:
                if is_live:
                    live(p,folder.parent,**args)
                else:
                    replay(p,iq,folder.parent,**args)
                mail.put("end",("saved",""))
            except Exception as exc:
                mail.put("end",("error",str(exc) or type(exc).__name__))
        self.worker=threading.Thread(target=receive,name="fleet-operator",daemon=True)
        self.worker.start()
        self.field.health_tick()
        self.controls()

    def finish(self):
        if self.worker:
            self.stop.set()
            self.field.health_tick()
            self.state.configure(text="Finishing")
            self.detail.configure(text="Draining IQ and finishing packet and recording writes…")
            self.controls()

    def new_run(self):
        if self.pending_new:
            return
        self.pending_new=True
        if self.job_kind=="sic":
            self.job_stop.set()
        if self.worker:
            self.finish()
        elif self.job:
            self.detail.configure(text="Finishing the export or score before creating the next run…")
        else:
            self.reset()
        self.controls()

    def reset(self):
        # Workers are joined before resetting. Their closures only reference the old mailbox.
        self.phase="ready"
        self.close_run_dialogs()
        self.footer_pinned=False
        self.path=self.open_path=self.journal=self.stop=self.terminal=None
        self.mail=Mailbox()
        self.snapshot={}
        self.cursor=JournalCursor()
        self.rf_panel.update_data({})
        self.release_model()
        self.replay_pause.clear()
        self.pause_button.configure(text="Pause replay")
        self.field.reset()
        self.generic.reset()
        self.views.select(0)
        self.source_box.configure(textvariable=self.mode)
        self.panels.reset()
        self.run_number+=1
        self.name.set(f"Bench run {self.run_number}")
        self.pending_new=False
        self.prr.grid_remove()
        for label in self.health.values():
            label.configure(text=DASH,style="Numeric.TLabel")
        self.health_state.configure(text="Ready",style="Field.TLabel")
        self.state.configure(text="Ready")
        self.elapsed.configure(text="00:00.0")
        self.settings_changed()
        self.panels.render(self.model,0)
        try:
            self.reserve()
            self.detail.configure(text=f"New run  ·  {self.path.name}")
        except OSError as exc:
            self.detail.configure(text=str(exc))
        self.controls()

    def progress(self,d):
        path=self.path/d["journal"]
        if path!=self.journal:
            if self.journal is not None:  # Final merged SIC journal has a new row order.
                self.cursor=JournalCursor()
                self.release_model(self.model.book.m)
                self.generic.reset()
                self.panels.log_floor=0
                self.panels.log.delete(*self.panels.log.get_children())
            self.journal=path
        self.snapshot=d
        self.rf_panel.update_data(d)
        if self.model.total==0 and d.get("mission"):
            self.model.book=PeriodBook(Mission.read(d["mission"]))
        self.panels.gaps=d.get("gaps",[])
        elapsed=d["capture_s"]
        self.elapsed.configure(text=clock_text(elapsed,True))
        is_live=self.mode.get()=="Live RTL-SDR"
        if not self.stop.is_set():
            self.state.configure(text=("Reconnecting…" if d.get("reconnecting") else "Receiving") if is_live else
                                 "Paused" if self.replay_pause.is_set() else ("SIC replay" if d["pass_name"]=="sic" else "Replaying"))
        if is_live:
            text="IQ stopped" if d["recorder_warning"] else ("● Recording" if self.setup.record.get() else "Off")
            self.recording.configure(text=text,style="Alert.TLabel" if text in ("● Recording","IQ stopped") else "Field.TLabel")
        else:
            self.recording.configure(text="Synthetic replay" if d["evidence"].startswith("synthetic") else "Recorded replay")
        lag=d["backlog_s"]
        drops=d["dropped_samples"]
        clip=d["clipped_fraction"]
        recorder=d["recorder_dropped_samples"]
        paced=is_live or self.setup.paced.get()
        offline=f"{d['dsp_realtime']:.1f}× real time" if d.get("dsp_realtime") else "Offline replay"
        state=("IQ loss" if drops or recorder else "Recording fault" if d["recorder_warning"] else
               "ADC clipping" if clip>CLIP_FRACTION else "Behind" if paced and lag>.26 else
               "Noise floor raised" if any(v["raised"] for v in d.get("rf_diagnostics",{}).get("floors",{}).values()) else
               "Check CFO margin" if any(v["warnings"] for v in d.get("cfo_margin",{}).get("carriers",{}).values()) else
               "Keeping up" if paced else offline)
        if d.get("clip_streak",0)>=1:
            state="Sustained clipping"
        if d["recorder_warning"]:
            state="Recording fault"
        self.health_state.configure(text=state,style="Alert.TLabel" if state in ("IQ loss","Behind","ADC clipping","Sustained clipping","Recording fault","Noise floor raised","Check CFO margin") else "Field.TLabel")
        metrics={"Backlog":f"{lag:.2f} s" if paced else DASH,
                 "Dropped IQ blocks":(f"{d['dropped_blocks']:,}  ·  {drops:,} smp" if d["dropped_blocks"] is not None
                                      else f"Unknown · {drops:,} smp") + f"  ·  {d.get('dropout_count',0)} dropouts",
                 "ADC clipping":f"{clip*100:.3f}%","Recorder drops":f"{recorder:,} smp" if is_live else DASH,
                 "Tuner gain":f"{d['gain_db']:g} dB" if d["gain_db"] is not None else DASH,
                 "Front end":short_name(d.get("front_end")),
                 "CRC passes · PHY / App":f"{d['counts'].get('phy_crc_ok/True',0)} / {d['counts'].get('application_crc_ok/True',0)}"}
        for key,text in metrics.items():
            alarm=(key=="ADC clipping" and clip>CLIP_FRACTION or key=="Backlog" and lag>.26
                   or key=="Dropped IQ blocks" and (drops or d.get("reconnecting")) or key=="Recorder drops" and recorder
                   or key=="Tuner gain" and d.get("gain_warning"))
            self.health[key].configure(text=text,style="Alert.TLabel" if alarm else "Numeric.TLabel")
        if d["recorder_warning"]:
            self.detail.configure(text=f"IQ recording stopped · {d['recorder_warning']}")
        elif not self.pending_new and not self.job and not self.footer_pinned:
            self.detail.configure(text=d.get("gain_warning") or f"{self.path.name}   ·   peak ADC clipping {d['peak_clip_fraction']*100:.3f}%")
        self.controls()

    def poll(self):
        if not self.mail.plan_warnings.empty():
            warnings = self.mail.plan_warnings.get_nowait()
            dialog = tk.Toplevel(self.root)
            dialog.title("Carrier plan warnings")
            dialog.transient(self.root)
            text = tk.Text(dialog, wrap="word", width=68, height=min(14, 3*len(warnings)+1))
            text.insert("1.0", "\n\n".join(warnings))
            text.configure(state="disabled")
            text.pack(fill="both", expand=True, padx=12, pady=8)
            ttk.Button(dialog, text="Close", command=dialog.destroy).pack(pady=(0, 8))
        gain,progress,end=self.mail.take()
        if gain and self.stop and not self.stop.is_set():
            self.state.configure(text="Setting gain")
            self.detail.configure(text=gain)
        if progress:
            self.progress(progress)
        if end:
            self.terminal=end
        behind=self.cursor.count<self.snapshot.get("committed",0)
        if behind:
            try:
                batch=self.cursor.read(self.journal,self.snapshot["committed"])
                for row in batch:
                    self.model.ingest(row)
            except (OSError,ValueError,KeyError,TypeError,csv.Error) as exc:
                if self.phase!="loading":
                    raise
                self.field.journal_warning=f"Journal read error after {self.model.total} rows · {exc}"
                self.snapshot["committed"]=self.model.total
                self.cursor.count=self.model.total
                batch=[]
            if not batch and self.phase=="loading":
                self.snapshot["committed"]=self.cursor.count
                self.field.journal_warning=self.field.journal_warning or "Journal ended before the saved summary count."
            self.update_fleet_counts()
        if progress or behind:
            self.panels.render(self.model,self.snapshot["capture_s"])
        if self.phase=="loading" and self.cursor.count>=self.snapshot["committed"]:
            self.panels.render(self.model,self.snapshot["capture_s"])
            self.field.loaded()
        if self.terminal and self.cursor.count>=self.snapshot.get("committed",0):
            self.worker.join()
            self.worker=None
            self.phase,error=self.terminal
            manifest=self.path/"summary.json"
            if manifest.is_file():
                self.field.summary=json.loads(manifest.read_text(encoding="utf-8"))
            self.terminal=None
            self.state.configure(text="Run saved" if self.phase=="saved" else "Run error")
            if self.mode.get()=="Live RTL-SDR":
                recorded=(self.path/"capture.iq").exists()
                fault=self.snapshot.get("recorder_warning")
                self.recording.configure(text="IQ stopped" if fault else "IQ saved" if recorded else "Off",
                                         style="Alert.TLabel" if fault else "Field.TLabel")
            if error:
                self.health_state.configure(text="Run error",style="Alert.TLabel")
                self.detail.configure(text=error)
                manifest=self.path/"summary.json"
                if json.loads(manifest.read_text(encoding="utf-8")).get("status")=="ready":
                    write_json(manifest,dict(run_id=self.path.name,run_name=self.name.get(),status="failed",error=error))
            elif not self.footer_pinned:
                suffix=self.snapshot.get("recorder_warning") or self.snapshot.get("gain_warning") or f"{self.model.total:,} packets"
                self.detail.configure(text=f"Saved {self.path.name}   ·   {suffix}")
            self.controls()
        outer=self.views.index("current")
        if outer==1:
            self.generic.render(self.model)
        elif outer==0:
            selected=self.panels.fleet.selection()
            self.panels.plots.refresh(self.model,int(selected[0]) if selected else None,self.snapshot.get("capture_s",0),gaps=self.panels.gaps)
        self.field.health_tick()
        if self.job_kind=="sic":
            _,d,_=self.sic_updates.take()
            if d:
                self.detail.configure(text=f"Offline SIC   ·   {d['pass_name']} pass   ·   {d['pass_fraction']:.0%} of recorded samples")
        self.finish_job()
        if not self.worker and not self.job:
            if self.closing:
                self.model.close()
                self.root.destroy()
                return
            if self.pending_new:
                self.reset()
        self.timer=self.root.after(35 if self.cursor.count<self.snapshot.get("committed",0) else 125,self.poll)

    def export_dialog(self):
        dlg=tk.Toplevel(self.root)
        dlg.title("Export CSV")
        dlg.transient(self.root)
        dlg.grab_set()
        box=ttk.Frame(dlg,padding=(18,14))
        box.pack()
        ttk.Label(box,text="Export CSV",style="Heading.TLabel").pack(anchor="w")
        ttk.Label(box,text=self.name.get(),style="Small.TLabel").pack(anchor="w",pady=(1,7))
        choice=tk.StringVar(value="all")
        for key,text in (("all","Every packet in this run"),("filtered","Packets matching the current filters"),
                         ("fleet","Fleet summary · one row per ChipSat"),("selected","Selected ChipSat history"),
                         ("sic","SIC-only trusted recoveries")):
            option=ttk.Radiobutton(box,text=text,value=key,variable=choice)
            option.pack(anchor="w")
            if key=="selected" and not self.panels.fleet.selection() or key=="sic" and not self.has_sic():
                option.state(["disabled"])
        if self.worker:
            ttk.Label(box,text="Uses packets committed through the latest receiver update.",style="Small.TLabel").pack()
        def save():
            kind=choice.get()
            dlg.destroy()
            self.choose_export(kind)
        ttk.Button(box,text="Choose file…",command=save).pack(anchor="e",pady=(8,0))

    def choose_export(self,kind):
        if not self.journal or self.job:
            return
        dest=self.path/"exports"
        try:
            dest.mkdir(exist_ok=True)
        except OSError:
            dest=self.path
        filename=filedialog.asksaveasfilename(parent=self.root,title="Save export",initialdir=dest,
            initialfile=f"{self.path.name}-{kind}.csv",defaultextension=".csv",filetypes=[("CSV","*.csv")])
        if filename:
            self.begin_export(kind,Path(filename))

    def has_sic(self):
        return self.field.label=="Replay + SIC" and self.phase=="saved"


    def run_job(self,operation,tag):
        if self.job or not self.path:
            return
        run_id=self.path.name
        self.job_kind=tag
        self.job_stop=threading.Event()
        self.sic_updates=Mailbox()
        def work():
            try:
                self.jobs.put((run_id,tag,operation(),None))
            except Exception as exc:
                self.jobs.put((run_id,tag,None,str(exc) or type(exc).__name__))
        self.job=threading.Thread(target=work,name="fleet-export" if tag=="export" else "fleet-score",daemon=True)
        self.job.start()
        self.controls()

    def begin_export(self,kind,filename):
        if self.journal is None:
            return
        journal,count=self.journal,self.snapshot["committed"]
        filters=self.panels.filters()
        if kind=="selected":
            sel=self.panels.fleet.selection()
            if not sel:
                return
            filters=Filters(sel[0])
        if kind=="generic":
            filters=self.generic.filters()
        if kind=="sic":
            filters=Filters()
            if self.field.comparison:
                journal=Path(self.field.comparison.get("sic_only_file",self.path/"sic_only.csv"))
                count=self.field.comparison["sic_only"]
        context=dict(run_id=self.field.summary.get("run_id") or self.path.name,run_name=self.name.get(),
                     timestamp_source="capture_relative_only")
        filename=Path(filename)
        def write():
            return filename,export_csv(journal,count,filename,kind,filters,context)
        self.run_job(write,"export")

    def score_dialog(self):
        truth=filedialog.askopenfilename(parent=self.root,title="Choose the transmit record",filetypes=[("CSV","*.csv")])
        if truth:
            offset=simpledialog.askfloat("TX clock offset","Capture seconds at transmit-log time zero",
                                         initialvalue=0.,parent=self.root)
            if offset is not None:
                self.begin_score(truth,offset)

    def begin_score(self,truth,offset=0.):
        if self.phase=="saved":
            folder=self.path
            self.run_job(lambda:score(folder,truth,offset),"score")

    def finish_job(self):
        try:
            run_id,tag,value,error=self.jobs.get_nowait()
        except queue.Empty:
            return
        self.job.join()
        self.job=None
        self.job_kind=None
        if self.path and run_id==self.path.name:
            self.footer_pinned=True
            if error:
                self.detail.configure(text=f"{tag.capitalize()} failed · {error}")
            elif tag=="export":
                filename,count=value
                self.open_path=filename.parent
                self.detail.configure(text=f"Exported {count:,} rows   ·   {filename}")
            elif tag=="sic":
                if value and not self.pending_new and not self.closing:
                    self.field.open_saved(value)
                    self.field.compare_dialog()
                else:
                    self.detail.configure(text="SIC cancelled · original run preserved")
            elif tag=="compare":
                self.field.comparison=value
                if not self.pending_new and not self.closing:
                    self.field.compare_dialog()
            else:
                offered=value["offered"]
                matched=value["matched"]
                if offered:
                    self.display_prr(value)
                else:
                    self.prr.grid_remove()
                self.detail.configure(text=f"Scored against {Path(value['transmit_log']).name}   ·   {matched}/{offered} matched")
                if self.model.book.m.epoch is None:
                    aligned=self.model.book.m.aligned({},value["tx_to_capture_offset_s"])
                    if aligned.origin==self.model.book.m.origin:
                        self.model.book.m=aligned
                    else:
                        self.field.open_saved(self.path,mission=aligned,keep_selection=True)
        self.controls()

    def display_prr(self,score):
        self.prr.configure(text=f"PRR  {score['matched']/score['offered']:.1%}  ({score['matched']}/{score['offered']})")
        self.prr.grid(row=0,column=7,rowspan=2,padx=(4,0))

    def open_folder(self,path=None):
        if path or self.open_path:
            try:
                os.startfile(str(path or self.open_path))
            except OSError as exc:
                messagebox.showerror("Open folder",str(exc),parent=self.root)

    def close(self):
        self.closing=True
        self.pending_new=False
        if self.job_kind=="sic":
            self.job_stop.set()
        if self.worker:
            self.finish()
        elif not self.job:
            self.root.after_cancel(self.timer)
            self.model.close()
            self.root.destroy()


def open_gui(initial_mode="Live",initial_iq="",saved=None,*,bench=False):
    root=tk.Tk()
    window=ReceiverWindow(root,initial_mode,initial_iq)
    if bench:
        window.setup.fleet_mode.set("Bench")
        window.setup.fleet_vars["size"].set("2")
        window.setup.fleet_vars["target"].set("2")
        window.settings_changed()
    if saved:
        window.field.open_saved(saved,mission=window.setup.mission() if bench else None)
    root.mainloop()
