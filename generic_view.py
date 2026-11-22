"""PHY-valid payload terminal shared by live, replay and archived journals."""
import tkinter as tk
from tkinter import ttk
from fleet_view import Filters
from operator_theme import mhz_label


def escaped(data):
    return "".join(chr(x) if 32<=x<=126 and x!=92 else "\\\\" if x==92 else f"\\x{x:02X}" for x in data)


def line(p):
    ms=round(p.t*1000)
    minute,rem=divmod(ms,60000)
    sec,frac=divmod(rem,1000)
    data=bytes.fromhex(p.row["payload_hex"])
    return f"{minute:02}:{sec:02}.{frac:03} | {mhz_label(p.fc)} MHz | SF{p.sf} | {len(data)} B | {data.hex(' ').upper()} | {escaped(data)}"


class GenericView:
    def __init__(self,parent,panels,export,mono):
        self.panels=panels
        self.pause=tk.BooleanVar()
        self.floor=0
        self.rows=[]
        self.model=None
        self.signature=None
        parent.columnconfigure(0,weight=1)
        parent.rowconfigure(2,weight=1)
        top=ttk.Frame(parent)
        top.grid(row=0,column=0,sticky="ew")
        ttk.Label(top,text="Generic LoRa",style="Heading.TLabel").pack(side="left")
        self.summary=ttk.Label(top,style="Small.TLabel")
        self.summary.pack(side="right")
        menu=ttk.Frame(parent)
        menu.grid(row=1,column=0,sticky="ew",pady=(5,7))
        self.fc=panels._combo(menu,"Carrier (MHz)",panels.fc_filter,11)
        self.sf=panels._combo(menu,"SF",panels.sf_filter,5)
        ttk.Checkbutton(menu,text="Pause autoscroll",variable=self.pause).pack(side="left")
        ttk.Button(menu,text="Copy payload",command=self.copy).pack(side="left",padx=4)
        ttk.Button(menu,text="Clear view",command=self.clear).pack(side="left")
        self.export_button=ttk.Button(menu,text="Export filtered CSV",command=export)
        self.export_button.pack(side="right")
        wrap=ttk.Frame(parent)
        wrap.grid(row=2,column=0,sticky="nsew")
        wrap.columnconfigure(0,weight=1)
        wrap.rowconfigure(0,weight=1)
        self.list=tk.Listbox(wrap,font=(mono,10),bg="#f8fafb",fg="#183740",selectbackground="#cee5df",
            selectforeground="#193d46",activestyle="none",relief="flat",exportselection=False)
        self.list.grid(row=0,column=0,sticky="nsew")
        sx=ttk.Scrollbar(wrap,orient="horizontal",command=self.list.xview)
        sy=ttk.Scrollbar(wrap,orient="vertical",command=self.list.yview)
        sx.grid(row=1,column=0,sticky="ew")
        sy.grid(row=0,column=1,sticky="ns")
        self.list.configure(xscrollcommand=sx.set,yscrollcommand=sy.set)
        self.list.bind("<Control-c>",lambda _:self.copy())
        self.settings=ttk.Label(parent,style="Small.TLabel")
        self.settings.grid(row=3,column=0,sticky="w",pady=(5,0))

    def configure(self,p):
        self.fc.configure(values=self.panels.fc_box.cget("values"))
        self.sf.configure(values=self.panels.sf_box.cget("values"))
        self.settings.configure(text=(f"PHY CRC pass   ·   {p.bandwidth_hz/1000:g} kHz   ·   sync 0x{p.sync_word:02X}   ·   "
                       f"{len(p.carriers_hz)} carriers   ·   SF {', '.join(map(str,p.spreading_factors))}") if p else "")

    def reset(self):
        self.floor=0
        self.model=None
        self.signature=None
        self.rows=[]
        self.list.delete(0,"end")
        self.pause.set(False)
        self.summary.configure(text="")

    def filters(self):
        f=self.panels.filters()
        return Filters("",f.fc,f.sf)

    def render(self,model):
        self.model=model
        f=self.filters()
        sig=(id(model),model.generic_total,self.floor,f)
        if self.signature==sig:
            return
        self.signature=sig
        selected=self.list.curselection()
        selected_id=self.rows[selected[0]][0] if selected else None
        scroll=self.list.yview()[0]
        self.rows=[(i,p) for i,p in model.generic if i>self.floor and f.matches(p)]
        self.list.delete(0,"end")
        if self.rows:
            self.list.insert("end",*(line(p) for _,p in self.rows))
        for j,(i,_) in enumerate(self.rows):
            if i==selected_id:
                self.list.selection_set(j)
        self.list.yview_moveto(scroll if self.pause.get() else 1)
        self.summary.configure(text=f"{len(self.rows)} displayed   ·   {model.generic_total:,} PHY-valid this run")

    def clear(self):
        self.floor=self.model.total if self.model else 0
        if self.model:
            self.render(self.model)

    def copy(self):
        sel=self.list.curselection()
        if sel:
            p=self.rows[sel[0]][1]
            self.list.clipboard_clear()
            self.list.clipboard_append(bytes.fromhex(p.row["payload_hex"]).hex(" ").upper())
