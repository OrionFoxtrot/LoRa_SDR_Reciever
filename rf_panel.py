"""Operator view of capture-band RF power and the decoder stages on each branch."""
import tkinter as tk
from tkinter import ttk
import numpy as np


class RFPanel(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=(12, 8))
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=3)
        self.rowconfigure(3, weight=2)
        self.floor = ttk.Label(self, text="Waiting for RF measurements")
        self.floor.grid(row=0, column=0, sticky="w")
        self.canvas = tk.Canvas(self, height=150, background="#15232d", highlightthickness=0)
        self.canvas.grid(row=1, column=0, sticky="nsew", pady=5)
        ttk.Label(self, text="Decoder stages by monitored carrier and spreading factor").grid(row=2, column=0, sticky="w")
        fields = ("branch", "syncs", "headers", "header_rejects", "unstable_headers", "phy_crc_pass",
                  "phy_crc_fail", "trusted_packets", "suspect_packets")
        headings = ("MHz / SF", "Syncs", "Headers", "Rejected", "Unstable", "PHY pass", "PHY fail", "Trusted", "Suspect timing")
        self.tree = ttk.Treeview(self, columns=fields, show="headings", height=5)
        for key, label in zip(fields, headings):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=112 if key=="branch" else 78, anchor="center")
        self.tree.grid(row=3, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(self, command=self.tree.yview)
        scroll.grid(row=3, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.cfo = ttk.Label(self, justify="left")
        self.cfo.grid(row=4,column=0,sticky="w",pady=(6,0))
        self.data = {}
        self.image = self.drawn = None
        self.canvas.bind("<Configure>", lambda _: self.render())
        self.bind("<Map>", lambda _: self.after_idle(self.render))

    def update_data(self, snapshot):
        self.data = snapshot
        self.render()

    def render(self):
        if not self.winfo_ismapped():
            return
        branches = self.data.get("branches", {})
        for key in set(self.tree.get_children())-branches.keys():
            self.tree.delete(key)
        for key, counts in branches.items():
            fc, sf = key.split("/")
            values = [f"{int(fc)/1e6:g} / {sf}", *[counts.get(k, "—") for k in self.tree["columns"][1:]]]
            if self.tree.exists(key):
                self.tree.item(key, values=values)
            else:
                self.tree.insert("", "end", iid=key, values=values)
        rf = self.data.get("rf_diagnostics", {})
        desense = rf.get("desense", {})
        levels = [f"{int(fc)/1e6:g} MHz   {v['dbfs']:.1f} dBFS" + ("   ↑ raised" if not desense and v["raised"] else "")
                  for fc,v in rf.get("floors", {}).items()]
        label = "Channel noise    " + "     ·     ".join(levels) if levels else "No recorded noise-floor estimate"
        if "dbfs" in desense:
            source = "Unused spectrum" if desense["source"] == "out_of_channel" else "In-channel fallback"
            label += f"\n{source}   {desense['dbfs']:.1f} dBFS" + ("   ↑ noise floor raised" if desense["raised"] else "")
        self.floor.configure(text=label)
        margin = self.data.get("cfo_margin",{})
        lines = []
        for fc, stats in margin.get("carriers",{}).items():
            text = f"{int(fc)/1e6:g} MHz    "
            if stats["count"]:
                text += (f"{stats['median_hz']/1000:+.1f} kHz median    "
                         f"{stats['p05_hz']/1000:+.1f} to {stats['p95_hz']/1000:+.1f} kHz (5–95%)    "
                         f"{stats['count']} packets")
                if stats["warnings"]:
                    text += (f"    ⚠  95th |CFO| {stats['p95_abs_hz']/1000:.1f} kHz"
                             f"    Check tuner adjustment {stats['suggested_delta_ppm']:+.1f} ppm")
            else:
                text += "No PHY-CRC-valid packets in this window"
            lines.append(text)
        self.cfo.configure(text="CFO over the last 10 capture minutes\n"+"\n".join(lines) if lines else "")
        rows = rf.get("waterfall", [])
        c = self.canvas
        width, height = c.winfo_width(), c.winfo_height()
        stamp = (rf.get("center_hz"), rows[-1][0] if rows else None, id(rows[-1]) if rows else None, width, height)
        if self.drawn == stamp:
            return
        self.drawn = stamp
        c.delete("all")
        if not rows or width < 120 or height < 60:
            c.create_text(8, 8, anchor="nw", fill="white", text="No capture-band waterfall available")
            return
        times = np.asarray([t for t,_ in rows])
        dt = rf["interval_s"]
        first, end = max(times[0]-dt, times[-1]-60), times[-1]
        w, h = width-75, height-29
        axis = np.linspace(first, end, h)
        index = np.minimum(times.searchsorted(axis), len(times)-1)
        bins = np.asarray([row for _,row in rows])
        power = bins[index[:, None], (np.arange(w)*bins.shape[1]//w)[None, :]]
        colors = ((8,19,39),(22,76,137),(32,173,170),(242,202,75),(255,251,219))
        grid = np.linspace(-90, -25, len(colors))
        rgb = np.stack([np.interp(power, grid, channel) for channel in zip(*colors)], axis=-1).astype(np.uint8)
        rgb[times[index]-axis > dt+.001] = (38,38,38)
        self.image = tk.PhotoImage(data=f"P6 {w} {h} 255\n".encode()+rgb.tobytes(), format="PPM")
        c.create_image(71, 2, anchor="nw", image=self.image)
        for y,t in ((3, first), (h-12, end)):
            c.create_text(65, y, anchor="ne", fill="white", text=f"{t:.1f} s")
        half = rf["sample_rate_hz"]/2
        for x,f,anchor in ((71,-half,"sw"),(71+w/2,0,"s"),(width-4,half,"se")):
            label = f"{(rf['center_hz']+f)/1e6:g} MHz"
            if f == 0:
                label += f"     ·     −90 to −25 dBFS per {rf['bin_hz']/1000:.2f} kHz bin"
            c.create_text(x, height-2, anchor=anchor, fill="white", text=label)
