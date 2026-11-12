"""Radio settings for the Windows operator. The same profile feeds every decoder."""
import tkinter as tk
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from config import DEFAULT_PROFILE, read_profile
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
    def __init__(self, root, output, mode, iq, mono, changed):
        self.root, self.mode, self.mono, self.changed = root, mode, mono, changed
        self.path = tk.StringVar(value=str(DEFAULT_PROFILE))
        self.output = tk.StringVar(value=str(output))
        self.capture = tk.StringVar(value=iq)
        self.center, self.extra = tk.StringVar(), tk.StringVar()
        self.gain, self.ppm = tk.StringVar(value="10"), tk.StringVar(value="0")
        self.auto, self.record = tk.BooleanVar(value=True), tk.BooleanVar(value=True)
        self.sic, self.paced = tk.BooleanVar(), tk.BooleanVar(value=True)
        self.carriers, self.sfs = {}, {}
        self.recorded_center = tk.StringVar()
        self.profile = None
        self.locked_controls=[]
        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.transient(root)
        self.win.title("Receiver Setup")
        self.win.protocol("WM_DELETE_WINDOW", self.hide)
        main = ttk.Frame(self.win, padding=(20,14))
        main.pack(fill="both", expand=True)
        ttk.Label(main, text="Receiver setup", style="Heading.TLabel").pack(anchor="w")
        self.tabs = ttk.Notebook(main)
        self.tabs.pack(fill="both", expand=True, pady=(8,4))
        self.radio = r = ttk.Frame(self.tabs, padding=(10,6))
        files = ttk.Frame(self.tabs, padding=(10,6))
        self.tabs.add(r, text="Radio")
        self.tabs.add(files, text="Files")
        r.columnconfigure(0, weight=1)
        files.columnconfigure(0, weight=1)
        self._path(files,0,"Receiver profile",self.path,"json")
        self._path(files,2,"Run output folder",self.output,"dir")
        self._path(files,4,"IQ replay file",self.capture,"iq")
        tuning = ttk.Frame(r)
        tuning.grid(row=0,column=0,sticky="ew")
        for i,(label,var) in enumerate((("Tuner center (MHz)",self.center),("Correction (ppm)",self.ppm))):
            ttk.Label(tuning,text=label,style="Field.TLabel").grid(row=0,column=i,sticky="w")
            e=ttk.Entry(tuning,textvariable=var,width=17)
            e.grid(row=1,column=i,sticky="w",padx=(0,12),pady=(3,0))
            if i==0:
                self.tuner=e
            else:
                self.ppm_input=e
        self.rate=ttk.Label(r,style="Small.TLabel")
        self.rate.grid(row=1,column=0,sticky="w",pady=(6,2))
        ttk.Label(r,text="Monitor carriers (MHz)",style="Field.TLabel").grid(row=2,column=0,sticky="w",pady=(6,2))
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
        self.live_box=ttk.Frame(r)
        self.live_box.grid(row=8,column=0,sticky="ew")
        ttk.Checkbutton(self.live_box,text="Auto-set gain before capture",variable=self.auto,
                        command=self.refresh).grid(row=0,column=0,columnspan=2,sticky="w")
        ttk.Label(self.live_box,text="Manual gain (dB)").grid(row=1,column=0,sticky="w")
        self.gain_input=ttk.Entry(self.live_box,textvariable=self.gain,width=8)
        self.gain_input.grid(row=1,column=1,sticky="w")
        ttk.Checkbutton(self.live_box,text="Record live IQ",variable=self.record,
                        command=self.refresh).grid(row=2,column=0,columnspan=2,sticky="w")
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
        for var in (self.center,self.extra):
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
        for w in self.choices.winfo_children():
            w.destroy()
        self.carriers.clear()
        for i,fc in enumerate(self.profile.carriers_hz):
            var=self.carriers[fc]=tk.BooleanVar(value=True)
            ttk.Checkbutton(self.choices,text=mhz_label(fc),variable=var,style="Choice.TCheckbutton",
                            command=self.refresh).grid(row=i//4,column=i%4,padx=(0,4))
        for sf,var in self.sfs.items():
            var.set(sf in self.profile.spreading_factors)
        self.extra.set("")
        self.center.set(mhz_label(self.profile.center_hz))
        self.refresh()

    def source_changed(self,*_):
        self.recorded_center.set("")
        live=self.mode.get()=="Live RTL-SDR"
        if not live and Path(self.capture.get()).is_file():
            try:
                fc=Layout.read(self.capture.get()).metadata.get("center_hz")
                if fc is not None:
                    self.recorded_center.set(mhz_label(int(fc)))
            except (OSError,ValueError,KeyError,TypeError):
                pass
        self.tuner.configure(textvariable=self.recorded_center if self.recorded_center.get() else self.center)
        self.tuner.state(["disabled"] if self.recorded_center.get() else ["!disabled"])
        self.ppm_input.state(["!disabled"] if live else ["disabled"])
        if live:
            self.live_box.grid()
            self.replay_box.grid_remove()
        else:
            self.live_box.grid_remove()
            self.replay_box.grid()
        self.refresh()

    def selected(self):
        if not self.profile:
            raise ValueError("Choose a valid receiver profile in Files.")
        carriers=[fc for fc,var in self.carriers.items() if var.get()]
        carriers.extend(parse_mhz(v) for v in self.extra.get().replace(","," ").split())
        if not carriers:
            raise ValueError("Choose at least one carrier.")
        return replace(self.profile,carriers_hz=tuple(carriers),
                       spreading_factors=tuple(sf for sf,v in self.sfs.items() if v.get()),
                       center_hz=parse_mhz(self.recorded_center.get() or self.center.get())).checked()

    def refresh(self,*_):
        self.gain_input.state(["disabled"] if self.auto.get() or self.locked_controls else ["!disabled"])
        try:
            p=self.selected()
        except ValueError as exc:
            self.band.delete("all")
            self.note.configure(text=str(exc))
            self.rate.configure(text="")
        else:
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
        try:
            self.selected()
        except ValueError as exc:
            messagebox.showerror("Receiver settings",str(exc),parent=self.win)
            return
        self.hide()
        self.changed()

    def hide(self):
        if self.win.grab_current()==self.win:
            self.win.grab_release()
        self.win.withdraw()
        self.unlock()

    def unlock(self):
        for child in self.locked_controls:
            child.state(["!disabled"])
        self.locked_controls.clear()

