"""Windows fleet operator. Tk reads committed packets in batches from a run-owned journal."""
import json
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from config import ROOT
from fleet_view import Fleet, JournalCursor, export_csv
from gain import CLIP_FRACTION
from operator_panels import DASH, FleetPanels, clock_text
from operator_theme import NAVY, PAPER, configure
from receiver_setup import ReceiverSetup
from reports import directory, score, write_json
from run import live, replay


class Mailbox:
    """Each run owns one mailbox. Only the newest health snapshot needs screen time."""
    def __init__(self):
        self.lock=threading.Lock()
        self.progress=self.gain=self.end=None

    def put(self,kind,value):
        with self.lock:
            setattr(self,kind,value)

    def take(self):
        with self.lock:
            messages=(self.gain,self.progress,self.end)
            self.gain=self.progress=self.end=None
        return messages


class ReceiverWindow:
    def __init__(self,root,initial_mode="Live",initial_iq=""):
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
        self._header()
        self._bar()
        body=ttk.Frame(root,style="Page.TFrame",padding=(12,9,12,7))
        body.grid(row=2,column=0,sticky="nsew")
        body.columnconfigure(0,weight=64,uniform="work")
        body.columnconfigure(1,weight=36,uniform="work")
        body.rowconfigure(0,weight=1)
        self.panels=FleetPanels(body,self.mono,self.score_dialog)
        self._health()
        self.setup=ReceiverSetup(root,ROOT/"runs",self.mode,initial_iq,self.mono,self.settings_changed)
        self.mode.trace_add("write",self.setup.source_changed)
        self.settings_changed()
        self.panels.render(self.model,0)
        self.controls()
        root.protocol("WM_DELETE_WINDOW",self.close)
        self.timer=root.after(100,self.poll)

    def _header(self):
        h=ttk.Frame(self.root,style="Header.TFrame",padding=(16,7))
        h.grid(row=0,column=0,sticky="ew")
        h.columnconfigure(2,weight=1)
        mark=tk.Canvas(h,width=37,height=37,bg=NAVY,highlightthickness=0)
        mark.grid(row=0,column=0,rowspan=2,padx=(0,11))
        mark.create_oval(3,3,33,33,outline="#86a8aa",width=1)
        mark.create_line(2,22,8,22,14,13,19,28,24,8,28,20,35,20,fill="#ddf4ed",width=2)
        ttk.Label(h,text="CORNELL  /  DeSCENT",style="Brand.TLabel").grid(row=0,column=1,sticky="w")
        ttk.Label(h,text="LoRa fleet receiver",style="Title.TLabel").grid(row=1,column=1,sticky="w")
        for c,title in ((3,"RUN STATUS"),(4,"IQ"),(5,"ELAPSED")):
            ttk.Label(h,text=title,style="HeaderMeta.TLabel").grid(row=0,column=c,sticky="w",padx=(23,0))
        self.state=ttk.Label(h,text="Ready",style="HeaderState.TLabel")
        self.recording=ttk.Label(h,style="HeaderState.TLabel")
        self.elapsed=ttk.Label(h,text="00:00.0",style="HeaderValue.TLabel")
        for c,w in ((3,self.state),(4,self.recording),(5,self.elapsed)):
            w.grid(row=1,column=c,sticky="w",padx=(23,0))

    def _bar(self):
        bar=ttk.Frame(self.root,padding=(15,6,15,8))
        bar.grid(row=1,column=0,sticky="ew")
        bar.columnconfigure(3,weight=1)
        for c,title in ((0,"Run name"),(1,"Source")):
            ttk.Label(bar,text=title,style="Small.TLabel").grid(row=0,column=c,sticky="w")
        self.name_input=ttk.Entry(bar,textvariable=self.name,width=30)
        self.name_input.grid(row=1,column=0,sticky="ew",padx=(0,9))
        self.source_box=ttk.Combobox(bar,textvariable=self.mode,values=("Live RTL-SDR","IQ replay"),
                                     width=13,state="readonly")
        self.source_box.grid(row=1,column=1,padx=(0,8))
        self.setup_button=ttk.Button(bar,text="Receiver Setup",command=self.show_setup)
        self.setup_button.grid(row=1,column=2,sticky="w")
        self.start_button=ttk.Button(bar,text="Start receiver",command=self.start_stop,style="Start.TButton")
        self.start_button.grid(row=1,column=4,padx=(6,0))
        self.new_button=ttk.Button(bar,text="New Run",command=self.new_run)
        self.new_button.grid(row=1,column=5,padx=6)
        self.export_button=ttk.Button(bar,text="Export CSV",command=self.export_dialog)
        self.export_button.grid(row=1,column=6)

    def _health(self):
        h=ttk.Frame(self.root,style="Card.TFrame",padding=(12,5))
        h.grid(row=3,column=0,sticky="ew",padx=12)
        self.health_state=ttk.Label(h,text="Ready",style="Field.TLabel")
        self.health_state.grid(row=1,column=0,sticky="w")
        self.health={}
        for c,title in enumerate(("Receiver health","Backlog","Dropped IQ blocks","ADC clipping","Recorder drops","Tuner gain","CRC passes · PHY / App")):
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

    def settings_changed(self):
        if self.setup is None or self.phase!="ready":
            return
        try:
            p=self.setup.selected()
        except ValueError:
            p=None
        self.panels.configure_channels(p)
        self.recording.configure(text=("Armed" if self.setup.record.get() else "Off") if self.mode.get()=="Live RTL-SDR"
                                 else "Playback",style="HeaderState.TLabel")
        self.health["Tuner gain"].configure(text=("Auto" if self.setup.auto.get() else f"{self.setup.gain.get()} dB")
                                           if self.mode.get()=="Live RTL-SDR" else DASH)
        if self.path is None:
            self.detail.configure(text="Choose a source and start a run.")
        if p:
            self.panels.render(self.model,0)

    def controls(self):
        ready=self.phase=="ready" and not self.pending_new
        busy=self.worker is not None
        self.name_input.state(["!disabled"] if ready else ["disabled"])
        self.source_box.configure(state="readonly" if ready else "disabled")
        self.setup_button.state(["disabled"] if self.pending_new else ["!disabled"])
        self.start_button.configure(text="Stop receiver" if busy else "Start receiver")
        self.start_button.state(["!disabled"] if ready or (busy and not self.stop.is_set()) else ["disabled"])
        self.new_button.state(["disabled"] if self.pending_new else ["!disabled"])
        available=bool(self.path and self.journal and self.job is None and not self.pending_new)
        self.export_button.state(["!disabled"] if available else ["disabled"])
        self.folder_button.state(["!disabled"] if self.open_path else ["disabled"])
        self.panels.score_button.state(["!disabled"] if available and self.phase=="saved" else ["disabled"])

    def reserve(self):
        parent=Path(self.setup.output.get()).expanduser().resolve()
        if self.path is None or self.path.parent!=parent:
            self.path=directory(parent)
            write_json(self.path/"summary.json",dict(run_id=self.path.name,run_name=self.name.get(),status="ready"))
        self.open_path=self.path

    def start_stop(self):
        self.finish() if self.worker else self.start()

    def show_setup(self):
        self.setup.show(locked=self.phase!="ready",gain=self.snapshot.get("gain_db"))

    def start(self):
        if self.phase!="ready" or self.worker or self.pending_new:
            return
        is_live=self.mode.get()=="Live RTL-SDR"
        try:
            p=self.setup.selected()
            if is_live:
                args=dict(gain=10. if self.setup.auto.get() else float(self.setup.gain.get()),
                          ppm=int(self.setup.ppm.get()),auto_gain=self.setup.auto.get(),record=self.setup.record.get())
            else:
                if not Path(self.setup.capture.get()).is_file():
                    picked=filedialog.askopenfilename(parent=self.root,title="Choose an IQ recording",
                                                      filetypes=[("Unsigned 8-bit IQ","*.iq"),("All files","*.*")])
                    if not picked:
                        return
                    self.setup.capture.set(picked)
                    p=self.setup.selected()
                args=dict(sic=self.setup.sic.get(),paced=self.setup.paced.get())
            self.reserve()
        except (ValueError,OSError) as exc:
            messagebox.showerror("Receiver settings",str(exc),parent=self.root)
            return
        self.setup.hide()
        folder=self.path
        iq=self.setup.capture.get()
        mail=self.mail
        self.stop=threading.Event()
        args.update(stop=self.stop,notify=lambda d:mail.put("progress",d),folder=folder,
                    name=self.name.get().strip() or folder.name)
        if is_live:
            args["gain_progress"]=lambda msg:mail.put("gain",msg)
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
        self.controls()

    def finish(self):
        if self.worker:
            self.stop.set()
            self.state.configure(text="Finishing")
            self.detail.configure(text="Draining IQ and finishing packet and recording writes…")
            self.controls()

    def new_run(self):
        if self.pending_new:
            return
        self.pending_new=True
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
        self.footer_pinned=False
        self.path=self.open_path=self.journal=self.stop=self.terminal=None
        self.mail=Mailbox()
        self.snapshot={}
        self.cursor=JournalCursor()
        self.model=Fleet()
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
                self.model=Fleet()
                self.panels.log_floor=0
                self.panels.log.delete(*self.panels.log.get_children())
            self.journal=path
        self.snapshot=d
        elapsed=d["capture_s"]
        self.elapsed.configure(text=clock_text(elapsed,True))
        is_live=self.mode.get()=="Live RTL-SDR"
        if not self.stop.is_set():
            self.state.configure(text="Receiving" if is_live else ("SIC replay" if d["pass_name"]=="sic" else "Replaying"))
        if is_live:
            text="IQ stopped" if d["recorder_warning"] else ("● Recording" if self.setup.record.get() else "Off")
            self.recording.configure(text=text,style="Recording.TLabel" if text in ("● Recording","IQ stopped") else "HeaderState.TLabel")
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
               "Keeping up" if paced else offline)
        self.health_state.configure(text=state,style="Alert.TLabel" if state in ("IQ loss","Behind","ADC clipping","Recording fault") else "Field.TLabel")
        metrics={"Backlog":f"{lag:.2f} s" if paced else DASH,
                 "Dropped IQ blocks":f"{d['dropped_blocks']:,}  ·  {drops:,} smp" if d["dropped_blocks"] is not None else f"Unknown · {drops:,} smp",
                 "ADC clipping":f"{clip*100:.3f}%","Recorder drops":f"{recorder:,} smp" if is_live else DASH,
                 "Tuner gain":f"{d['gain_db']:g} dB" if d["gain_db"] is not None else DASH,
                 "CRC passes · PHY / App":f"{d['counts'].get('phy_crc_ok/True',0)} / {d['counts'].get('application_crc_ok/True',0)}"}
        for key,text in metrics.items():
            alarm=(key=="ADC clipping" and clip>CLIP_FRACTION or key=="Backlog" and lag>.26
                   or key=="Dropped IQ blocks" and drops or key=="Recorder drops" and recorder)
            self.health[key].configure(text=text,style="Alert.TLabel" if alarm else "Numeric.TLabel")
        if d["recorder_warning"]:
            self.detail.configure(text=f"IQ recording stopped · {d['recorder_warning']}")
        elif not self.pending_new and not self.job and not self.footer_pinned:
            self.detail.configure(text=f"{self.path.name}   ·   peak ADC clipping {d['peak_clip_fraction']*100:.3f}%")
        self.controls()

    def poll(self):
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
            for row in self.cursor.read(self.journal,self.snapshot["committed"]):
                self.model.ingest(row)
        if progress or behind:
            self.panels.render(self.model,self.snapshot["capture_s"])
        if self.terminal and self.cursor.count>=self.snapshot.get("committed",0):
            self.worker.join()
            self.worker=None
            self.phase,error=self.terminal
            self.terminal=None
            self.state.configure(text="Run saved" if self.phase=="saved" else "Run error")
            if self.mode.get()=="Live RTL-SDR":
                recorded=(self.path/"capture.iq").exists()
                fault=self.snapshot.get("recorder_warning")
                self.recording.configure(text="IQ stopped" if fault else "IQ saved" if recorded else "Off",
                                         style="Recording.TLabel" if fault else "HeaderState.TLabel")
            if error:
                self.health_state.configure(text="Run error",style="Alert.TLabel")
                self.detail.configure(text=error)
                manifest=self.path/"summary.json"
                if json.loads(manifest.read_text(encoding="utf-8")).get("status")=="ready":
                    write_json(manifest,dict(run_id=self.path.name,run_name=self.name.get(),status="failed",error=error))
            elif not self.footer_pinned:
                suffix=self.snapshot.get("recorder_warning") or f"{self.model.total:,} packets"
                self.detail.configure(text=f"Saved {self.path.name}   ·   {suffix}")
            self.controls()
        self.finish_job()
        if not self.worker and not self.job:
            if self.closing:
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
                         ("fleet","Fleet summary · one row per ChipSat")):
            ttk.Radiobutton(box,text=text,value=key,variable=choice).pack(anchor="w")
        if self.worker:
            ttk.Label(box,text="Uses packets committed through the latest receiver update.",style="Small.TLabel").pack()
        def save():
            kind=choice.get()
            dlg.destroy()
            directory=self.path/"exports"
            directory.mkdir(exist_ok=True)
            filename=filedialog.asksaveasfilename(parent=self.root,title="Save export",initialdir=directory,
                initialfile=f"{self.path.name}-{kind}.csv",defaultextension=".csv",filetypes=[("CSV","*.csv")])
            if filename:
                self.begin_export(kind,Path(filename))
        ttk.Button(box,text="Choose file…",command=save).pack(anchor="e",pady=(8,0))

    def run_job(self,operation,tag):
        if self.job or not self.path:
            return
        run_id=self.path.name
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
        filename=Path(filename)
        def write():
            return filename,export_csv(journal,count,filename,kind,filters)
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
        if self.path and run_id==self.path.name:
            self.footer_pinned=True
            if error:
                self.detail.configure(text=f"{tag.capitalize()} failed · {error}")
            elif tag=="export":
                filename,count=value
                self.open_path=filename.parent
                self.detail.configure(text=f"Exported {count:,} rows   ·   {filename.name}")
            else:
                offered=value["offered"]
                matched=value["matched"]
                if offered:
                    self.prr.configure(text=f"PRR  {matched/offered:.1%}  ({matched}/{offered})")
                    self.prr.grid(row=0,column=7,rowspan=2,padx=(4,0))
                else:
                    self.prr.grid_remove()
                self.detail.configure(text=f"Scored against {Path(value['transmit_log']).name}   ·   {matched}/{offered} matched")
        self.controls()

    def open_folder(self):
        if self.open_path:
            try:
                os.startfile(str(self.open_path))
            except OSError as exc:
                messagebox.showerror("Open folder",str(exc),parent=self.root)

    def close(self):
        self.closing=True
        self.pending_new=False
        if self.worker:
            self.finish()
        elif not self.job:
            self.root.after_cancel(self.timer)
            self.root.destroy()


def open_gui(initial_mode="Live",initial_iq=""):
    root=tk.Tk()
    ReceiverWindow(root,initial_mode,initial_iq)
    root.mainloop()
