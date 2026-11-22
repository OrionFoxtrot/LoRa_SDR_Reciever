"""Radio settings for the Windows operator. The same profile feeds every decoder."""
import tkinter as tk
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from config import (DEFAULT_PROFILE, MONITOR_CENTER_HZ, MONITOR_PROFILES, MONITOR_CHOICES, MONITOR_SFS, EventIQLimits,
                    describe_front_end, monitor_profile_name, read_profile, search_half_width)
from front_end import FRONT_END_LABELS, FrontEndPreferences, from_fields, preset
from mission import Mission, read_roster
from operator_theme import NAVY, draw_band, mhz_label
from sources import Layout


def parse_mhz(text):
    try:
        freq = Decimal(text) * 1_000_000
        if freq.is_finite() and 0 < freq < 2**32 and freq == int(freq):
            return int(freq)
    except InvalidOperation:
        pass
    raise ValueError(f"Enter a frequency in MHz with whole-Hz precision. Received {text!r}.")


class ReceiverSetup:
    def __init__(self, root, output, mode, iq, mono, changed, front_preferences=None):
        self.root, self.mode, self.mono, self.changed = root, mode, mono, changed
        self.preferences = front_preferences or FrontEndPreferences()
        choice,front_metadata = self.preferences.selected()
        self.recorded_front_end = None
        self.path = tk.StringVar(value=str(DEFAULT_PROFILE))
        self.output = tk.StringVar(value=str(output))
        self.capture = tk.StringVar(value=iq)
        self.center, self.extra = tk.StringVar(), tk.StringVar()
        self.gain, self.ppm = tk.StringVar(value="10"), tk.StringVar(value="0")
        self.max_cfo = tk.StringVar()
        self.auto, self.record = tk.BooleanVar(value=True), tk.BooleanVar(value=True)
        self.sic, self.paced = tk.BooleanVar(), tk.BooleanVar(value=True)
        self.carriers, self.sfs = {}, {}
        self.updating_channels = False
        self.custom_selected = self.recorded_plan = False
        self.from_recording = False
        self.carrier_name = tk.StringVar()
        self.recorded_center = tk.StringVar()
        self.profile = None
        self.locked_controls=[]
        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.transient(root)
        self.win.title("Receiver Setup")
        self.win.protocol("WM_DELETE_WINDOW", self.done)
        main = ttk.Frame(self.win, padding=(20,14))
        main.pack(fill="both", expand=True)
        ttk.Label(main, text="Receiver setup", style="Heading.TLabel").pack(anchor="w")
        self.tabs = ttk.Notebook(main)
        self.tabs.pack(fill="both", expand=True, pady=(8,4))
        self.radio = r = ttk.Frame(self.tabs, padding=(10,6))
        files = ttk.Frame(self.tabs, padding=(10,6))
        self.tabs.add(r, text="Radio")
        self.tabs.add(files, text="Files")
        field=ttk.Frame(self.tabs,padding=(10,8))
        self.tabs.add(field,text="Fleet & station")
        front = ttk.Frame(self.tabs, padding=(10,8))
        self.tabs.add(front, text="Front end")
        self.front_fields = ttk.Frame(front)
        self.front_fields.pack(anchor="w")
        self.front_note = ttk.Label(front, wraplength=360)
        self.front_note.pack(anchor="w", pady=8)
        ttk.Label(self.front_fields,text="Front end").grid(row=0,column=0,sticky="w",pady=5)
        self.front_picker = ttk.Combobox(self.front_fields, values=tuple(FRONT_END_LABELS.values()),
                                         state="readonly",width=29)
        self.front_picker.set(FRONT_END_LABELS[choice])
        self.front_picker.grid(row=0,column=1,sticky="w")
        self.front_picker.bind("<<ComboboxSelected>>",self.select_front_end)
        self.front_vars, self.front_entries = {}, {}
        for i,(key,label) in enumerate((("lna_model","LNA model (or None)"),("nominal_gain_db","Nominal gain (dB)"),
                                        ("power_source","Power source"),("usb_supply","USB supply"),("filter","RF filter")),1):
            value = front_metadata[key]
            var = self.front_vars[key] = tk.StringVar(value="" if value is None else value)
            ttk.Label(self.front_fields,text=label).grid(row=i,column=0,sticky="w",padx=(0,8),pady=3)
            entry = self.front_entries[key] = ttk.Entry(self.front_fields,textvariable=var,width=28,
                                                       state="normal" if choice=="custom" else "readonly")
            entry.grid(row=i,column=1,sticky="w")
        self.fleet_mode=tk.StringVar(value="Mission")
        self.fleet_vars={}
        picker=ttk.Combobox(field,textvariable=self.fleet_mode,values=("Mission","Bench","Custom"),state="readonly",width=17)
        picker.grid(row=0,column=1,sticky="w")
        ttk.Label(field,text="Coverage preset").grid(row=0,column=0,sticky="w")
        for i,(key,label,default) in enumerate((("size","Fleet size","100"),("target","Target IDs","80"),
                ("period","Period (s)","30"),("epoch","Period epoch (capture s)",""),
                ("lat","Station latitude (°)",""),("lon","Station longitude (°)","")),1):
            var=self.fleet_vars[key]=tk.StringVar(value=default)
            ttk.Label(field,text=label).grid(row=i,column=0,sticky="w",pady=3,padx=(0,8))
            ttk.Entry(field,textvariable=var,width=16).grid(row=i,column=1,sticky="w")
        def preset(_):
            name=self.fleet_mode.get()
            if name!="Custom":
                for key,val in zip(("size","target","period"),("2","2","30") if name=="Bench" else ("100","80","30")):
                    self.fleet_vars[key].set(val)
        picker.bind("<<ComboboxSelected>>",preset)
        self.roster_path=tk.StringVar()
        self._path(field,7,"Optional roster CSV",self.roster_path,"csv")
        ttk.Label(field,text="A blank epoch uses recording alignment. A blank station uses the first valid run fix.",
                  style="Small.TLabel",wraplength=365).grid(row=9,column=0,columnspan=2,sticky="w",pady=8)
        self.roster_key=None
        self.roster={}
        r.columnconfigure(0, weight=1)
        files.columnconfigure(0, weight=1)
        self._path(files,0,"Receiver profile",self.path,"json")
        self._path(files,2,"Run output folder",self.output,"dir")
        self._path(files,4,"IQ replay file",self.capture,"iq")
        limits = EventIQLimits()
        self.event_hour = tk.StringVar(value=str(limits.per_kind_hour))
        self.event_mb = tk.StringVar(value=str(limits.max_mb))
        for row,label,var in ((6,"Event IQ clips per kind per rolling hour",self.event_hour),
                              (7,"Total event IQ and sidecars per run (MB)",self.event_mb)):
            ttk.Label(files,text=label).grid(row=row,column=0,sticky="w",pady=(6,0))
            ttk.Entry(files,textvariable=var,width=8).grid(row=row,column=1,sticky="w")
        tuning = ttk.Frame(r)
        tuning.grid(row=0,column=0,sticky="ew")
        for i,(label,var) in enumerate((("Tuner center (MHz)",self.center),("Correction (ppm)",self.ppm),
                                        ("CFO half-width (Hz)",self.max_cfo))):
            ttk.Label(tuning,text=label,style="Field.TLabel").grid(row=0,column=i,sticky="w")
            e=ttk.Entry(tuning,textvariable=var,width=17)
            e.grid(row=1,column=i,sticky="w",padx=(0,12),pady=(3,0))
            if i==0:
                self.tuner=e
            elif i==1:
                self.ppm_input=e
        self.rate=ttk.Label(r,style="Small.TLabel")
        self.rate.grid(row=1,column=0,sticky="w",pady=(6,2))
        self.carrier_picker = ttk.Combobox(r,textvariable=self.carrier_name,values=MONITOR_CHOICES,
                                          width=20,state="readonly")
        self.carrier_picker.grid(row=2,column=0,sticky="w",pady=(6,2))
        self.carrier_picker.bind("<<ComboboxSelected>>",lambda _:self.apply_monitor_profile(self.carrier_name.get()))
        self.choices=ttk.Frame(r)
        self.choices.grid(row=3,column=0,sticky="w")
        extra=ttk.Frame(r)
        extra.grid(row=4,column=0,sticky="ew",pady=4)
        ttk.Label(extra,text="Additional MHz").pack(side="left",padx=(0,8))
        ttk.Entry(extra,name="additional",textvariable=self.extra,width=22).pack(side="left")
        ttk.Label(r,text="Spreading factors",style="Field.TLabel").grid(row=5,column=0,sticky="w",pady=(6,2))
        factors=ttk.Frame(r)
        factors.grid(row=6,column=0,sticky="w")
        for sf in range(7,13):
            v=self.sfs[sf]=tk.BooleanVar()
            ttk.Checkbutton(factors,text=f"SF{sf}",variable=v,style="Choice.TCheckbutton",
                            command=self.refresh).pack(side="left",padx=(0,3))
        self.band=tk.Canvas(r,width=370,height=57,background=NAVY,highlightthickness=0)
        self.band.grid(row=7,column=0,sticky="ew",pady=(10,4))
        self.band.bind("<Configure>",self.refresh)
        self.replay_box=ttk.Frame(r)
        self.replay_box.grid(row=8,column=0,sticky="ew")
        ttk.Checkbutton(self.replay_box,text="Replay at capture speed",variable=self.paced).pack(anchor="w")
        ttk.Checkbutton(self.replay_box,text="Second pass with interference cancellation (SIC)",
                        variable=self.sic).pack(anchor="w")
        self.note=ttk.Label(r,style="Small.TLabel",wraplength=390)
        self.note.grid(row=9,column=0,sticky="w",pady=(3,0))
        ttk.Button(main,text="Done",command=self.done).pack(anchor="e",pady=(6,0))
        self.path.trace_add("write",self.load)
        self.capture.trace_add("write",self.source_changed)
        for var in (self.center,self.extra,self.max_cfo):
            var.trace_add("write",self.refresh)
        self.load()
        self.source_changed()

    def _path(self,frame,row,label,var,kind):
        ttk.Label(frame,text=label,style="Field.TLabel").grid(row=row,column=0,columnspan=2,sticky="w",pady=(6,2))
        ttk.Entry(frame,textvariable=var,width=35).grid(row=row+1,column=0,sticky="ew",padx=(0,5))
        def choose():
            result=(filedialog.askdirectory(parent=self.win) if kind=="dir" else
                    filedialog.askopenfilename(parent=self.win,filetypes=[(kind.upper(),f"*.{kind}"),("All files","*.*")]))
            if result:
                var.set(result)
        ttk.Button(frame,text="Browse…",command=choose).grid(row=row+1,column=1)

    def load(self,*_):
        try:
            self.profile=read_profile(self.path.get())
        except (ValueError,OSError,KeyError,TypeError):
            self.profile=None
            self.refresh()
            return
        self.from_recording = self.recorded_plan = False
        self.custom_selected = False
        if Path(self.path.get()) == DEFAULT_PROFILE:
            self.profile = self.preferences.carrier_profile(self.profile)
            self.custom_selected = (self.preferences.read("carrier_selection") or {}).get("name")=="Custom"
        self.load_channels()

    def load_channels(self):
        self.max_cfo.set(str(self.profile.max_cfo_hz))
        for w in self.choices.winfo_children():
            w.destroy()
        self.carriers.clear()
        available=set(self.profile.carriers_hz).union(*MONITOR_PROFILES.values())
        for i,fc in enumerate(sorted(available)):
            var=self.carriers[fc]=tk.BooleanVar()
            ttk.Checkbutton(self.choices,text=mhz_label(fc),variable=var,style="Choice.TCheckbutton",
                            command=self.refresh).grid(row=i//4,column=i%4,padx=(0,4))
        self.set_channels(self.profile.carriers_hz, self.profile.spreading_factors, self.profile.center_hz)

    def apply_monitor_profile(self, name):
        self.custom_selected = name == "Custom"
        self.recorded_plan = False
        if self.custom_selected:
            self.refresh()
        else:
            self.profile = replace(self.profile,monitor_only_hz=())
            self.set_channels(MONITOR_PROFILES[name], MONITOR_SFS, MONITOR_CENTER_HZ)

    def set_channels(self, carriers, sfs, center_hz):
        self.updating_channels = True
        try:
            for hz, variable in self.carriers.items():
                variable.set(hz in carriers)
            for sf, variable in self.sfs.items():
                variable.set(sf in sfs)
            self.extra.set(" ".join(mhz_label(fc) for fc in carriers if fc not in self.carriers))
            self.center.set(mhz_label(center_hz))
        finally:
            self.updating_channels = False
        self.refresh()

    def source_changed(self,*_):
        self.recorded_center.set("")
        live=self.mode.get()=="Live RTL-SDR"
        meta={}
        if not live and Path(self.capture.get()).is_file():
            try:
                layout=Layout.read(self.capture.get())
                meta=layout.metadata
                recorded = layout.recorded_profile(self.capture.get())
                if recorded and Path(self.path.get()) == DEFAULT_PROFILE:
                    self.profile = recorded
                    self.custom_selected = False
                    self.from_recording = self.recorded_plan = True
                    self.load_channels()
                elif self.from_recording:
                    self.load()
                fc=meta.get("center_hz")
                if fc is not None:
                    self.recorded_center.set(mhz_label(int(fc)))
            except (OSError,ValueError,KeyError,TypeError):
                pass
        self.tuner.configure(textvariable=self.recorded_center if self.recorded_center.get() else self.center)
        self.tuner.state(["disabled"] if self.recorded_center.get() else ["!disabled"])
        self.ppm_input.state(["!disabled"] if live else ["disabled"])
        self.recorded_front_end = meta.get("front_end")
        if live:
            if self.from_recording:
                self.load()
            self.replay_box.grid_remove()
            self.front_fields.pack(anchor="w",before=self.front_note)
            self.front_note.configure(text="Choose Custom to edit the hardware, USB supply or RF filter.")
        else:
            self.replay_box.grid()
            self.front_fields.pack_forget()
            self.front_note.configure(text=describe_front_end(meta.get("front_end")))
        self.refresh()

    def front_end(self):
        return from_fields({k:v.get() for k,v in self.front_vars.items()})

    def front_choice(self):
        return tuple(FRONT_END_LABELS)[self.front_picker.current()]

    def select_front_end(self, *_):
        choice = self.front_choice()
        if choice != "custom":
            for key,value in preset(choice).items():
                self.front_vars[key].set("" if value is None else value)
        for entry in self.front_entries.values():
            entry.configure(state="normal" if choice=="custom" else "readonly")
        self.changed()

    def save_front(self):
        selected = self.front_choice(),self.front_end()
        if selected != self.preferences.selected():
            self.preferences.save(*selected)

    def event_limits(self):
        try:
            return EventIQLimits(int(self.event_hour.get()),int(self.event_mb.get()))
        except ValueError as exc:
            raise ValueError("Enter nonnegative whole numbers for the event IQ limits in Files.") from exc

    def selected_carriers(self):
        carriers=[fc for fc,var in self.carriers.items() if var.get()]
        carriers.extend(parse_mhz(v) for v in self.extra.get().replace(","," ").split())
        if not carriers:
            raise ValueError("Choose at least one carrier.")
        return tuple(carriers)

    def save_carriers(self):
        if not self.recorded_plan:
            self.preferences.save_carriers(self.selected(),self.carrier_name.get())

    def selected(self):
        if not self.profile:
            raise ValueError("Choose a valid receiver profile in Files.")
        carriers=self.selected_carriers()
        width = self.profile.max_cfo_hz
        if self.max_cfo.get().strip() != str(width):
            width = search_half_width(self.max_cfo.get())
        return replace(self.profile,carriers_hz=carriers,
                       max_cfo_hz=width,
                       monitor_only_hz=tuple(fc for fc in self.profile.monitor_only_hz if fc in carriers),
                       spreading_factors=tuple(sf for sf,v in self.sfs.items() if v.get()),
                       center_hz=parse_mhz(self.recorded_center.get() or self.center.get())).checked()

    def refresh(self,*_):
        if self.updating_channels:
            return
        try:
            p=self.selected()
        except ValueError as exc:
            self.band.delete("all")
            self.note.configure(text=str(exc))
            self.rate.configure(text="")
            self.carrier_name.set("Custom")
        else:
            name = "Custom" if self.custom_selected or p.center_hz != MONITOR_CENTER_HZ else monitor_profile_name(p.carriers_hz)
            self.carrier_name.set(name)
            draw_band(self.band,p.center_hz,p.sample_rate_hz,p.bandwidth_hz,p.carriers_hz,self.mono)
            self.rate.configure(text=f"{p.sample_rate_hz/1e6:g} MS/s   ·   {p.bandwidth_hz/1000:g} kHz bandwidth")
            if not self.locked_controls:
                self.note.configure(text=f"{len(p.carriers_hz)*len(p.spreading_factors)} active decoders")
        self.changed()

    def show(self,files=False,locked=False,gain=None):
        self.unlock()
        if locked:
            def disable(parent):
                for child in parent.winfo_children():
                    if isinstance(child,(ttk.Entry,ttk.Button,ttk.Checkbutton)) and "disabled" not in child.state():
                        child.state(["disabled"])
                        self.locked_controls.append(child)
                    disable(child)
            disable(self.tabs)
            actual=f"   ·   Applied gain {gain:g} dB" if gain is not None else ""
            self.note.configure(text=f"Settings for this run{actual}")
        else:
            self.refresh()
        self.tabs.select(1 if files else 0)
        self.win.deiconify()
        self.win.lift()
        self.win.grab_set()

    def done(self):
        if self.locked_controls:
            self.hide()
            return
        try:
            self.selected()
            self.mission()
            self.event_limits()
            if self.mode.get()=="Live RTL-SDR":
                self.save_front()
            self.save_carriers()
        except (ValueError,OSError,TypeError) as exc:
            messagebox.showerror("Receiver settings",str(exc),parent=self.win)
            return
        self.hide()
        self.changed()

    def mission(self):
        v={k:var.get().strip() for k,var in self.fleet_vars.items()}
        path=self.roster_path.get().strip()
        key=(path,Path(path).stat().st_mtime_ns) if path else None
        if key!=self.roster_key:
            self.roster=read_roster(path) if path else {}
            self.roster_key=key
        station=(float(v["lat"]),float(v["lon"])) if v["lat"] or v["lon"] else None
        return Mission(int(v["size"]),float(v["period"]),int(v["target"]),
                       float(v["epoch"]) if v["epoch"] else None,"mission-aligned" if v["epoch"] else "capture-aligned",
                       station,self.roster).checked()

    def hide(self):
        if self.win.grab_current()==self.win:
            self.win.grab_release()
        self.win.withdraw()
        self.unlock()

    def unlock(self):
        for child in self.locked_controls:
            child.state(["!disabled"])
        self.locked_controls.clear()

