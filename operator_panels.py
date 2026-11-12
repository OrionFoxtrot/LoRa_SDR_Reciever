"""Fleet table, selected telemetry, packet log and carrier activity for the Tk operator."""
import tkinter as tk
from tkinter import ttk

from fleet_view import Filters, LOG_LIMIT, STALE_SECONDS, crc
from operator_theme import PANEL, RED, mhz_label

DASH = "—"


def clock_text(seconds, tenths=False):
    ticks = round(max(0, seconds)*10) if tenths else int(max(0, seconds))*10
    minutes, second = divmod(ticks//10, 60)
    base = f"{minutes:02d}:{second:02d}"
    return f"{base}.{ticks%10}" if tenths else base


def crc_text(value):
    flag = crc(value)
    return DASH if flag is None else ("Pass" if flag else "Fail")


def table(parent, cols, height=5, command=None):
    tree=ttk.Treeview(parent, columns=[c[0] for c in cols],show="headings",height=height,selectmode="browse")
    for key,label,width in cols:
        align="e" if key in ("last","age","count","carrier","soc","t","time","seq","n") else "center"
        tree.heading(key,text=label,anchor=align,
                     command=(lambda k=key:command(k)) if command else "")
        tree.column(key,width=width,minwidth=min(40,width),anchor=align)
    scroll=ttk.Scrollbar(parent,orient="vertical",command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    tree.pack(side="left",fill="both",expand=True)
    scroll.pack(side="right",fill="y")
    tree.tag_configure("stale",foreground="#866126")
    tree.tag_configure("alternate",background="#f5f8f8")
    tree.tag_configure("error",foreground=RED)
    return tree


class FleetPanels:
    columns=(("id","ChipSat ID",82),("last","Last heard",88),("age","Age (s)",62),
             ("count","Packets",64),("carrier","Carrier (MHz)",99),("sf","SF",44),("soc","SoC (%)",68))

    def __init__(self, parent, mono, score):
        self.mono=mono
        self.key,self.reverse="id",False
        self.selected_id=None
        self.log_floor=0
        self.carriers=()
        self.profile_key=None
        self.model=None
        self.now=0.
        self.id_filter,self.fc_filter,self.sf_filter=tk.StringVar(),tk.StringVar(value="All"),tk.StringVar(value="All")
        self.pause=tk.BooleanVar()
        left=ttk.Frame(parent,style="Page.TFrame")
        left.grid(row=0,column=0,sticky="nsew",padx=(0,10))
        left.rowconfigure(0,weight=1)
        left.columnconfigure(0,weight=1)
        book=ttk.Notebook(left)
        book.grid(row=0,column=0,sticky="nsew")
        fleet=ttk.Frame(book,padding=(11,7,11,8))
        log=ttk.Frame(book,padding=(11,7,11,8))
        book.add(fleet,text="Fleet")
        book.add(log,text="Packet log")
        for w in (fleet,log):
            w.columnconfigure(0,weight=1)
        fleet.rowconfigure(2,weight=1)
        head=ttk.Frame(fleet)
        head.grid(row=0,column=0,sticky="ew")
        ttk.Label(head,text="ChipSats heard",style="Heading.TLabel").pack(side="left")
        self.totals=ttk.Label(head,style="Small.TLabel")
        self.totals.pack(side="right")
        filters=ttk.Frame(fleet)
        filters.grid(row=1,column=0,sticky="ew",pady=(5,7))
        ttk.Label(filters,text="ID",style="Small.TLabel").pack(side="left")
        self.search=ttk.Entry(filters,textvariable=self.id_filter,width=7)
        self.search.pack(side="left",padx=(4,12))
        self.fc_box=self._combo(filters,"Carrier (MHz)",self.fc_filter,10)
        self.sf_box=self._combo(filters,"SF",self.sf_filter,4)
        ttk.Button(filters,text="Reset filters",command=self.clear_filters).pack(side="right")
        frame=ttk.Frame(fleet)
        frame.grid(row=2,column=0,sticky="nsew")
        self.fleet=table(frame,self.columns,5,self.sort_by)
        self.fleet.bind("<<TreeviewSelect>>",self.select)
        self.empty=ttk.Label(frame,text="Waiting for a valid ChipSat packet",style="Small.TLabel")
        log.rowconfigure(1,weight=1)
        tools=ttk.Frame(log)
        tools.grid(row=0,column=0,sticky="ew",pady=(0,5))
        ttk.Checkbutton(tools,text="Pause autoscroll",variable=self.pause).pack(side="left")
        ttk.Button(tools,text="Copy row",command=self.copy_row).pack(side="left",padx=4)
        ttk.Button(tools,text="Clear view",command=self.clear_log).pack(side="left")
        self.score_button=ttk.Button(tools,text="Score with TX log…",command=score)
        self.score_button.pack(side="right")
        frame=ttk.Frame(log)
        frame.grid(row=1,column=0,sticky="nsew")
        self.log=table(frame,(("n","Row",44),("t","Run time (s)",94),("id","ChipSat",58),("fc","MHz",80),
                              ("sf","SF",36),("phy","PHY CRC",62),("app","App CRC",62),("seq","Counter",64)),6)
        self.log_note=ttk.Label(log,style="Small.TLabel")
        self.log_note.grid(row=2,column=0,sticky="w",pady=(4,0))
        self.channel_card=ttk.Frame(left,style="Card.TFrame",padding=(12,6,12,8))
        self.channel_card.grid(row=1,column=0,sticky="ew",pady=(8,0))
        h=ttk.Frame(self.channel_card)
        h.pack(fill="x")
        ttk.Label(h,text="Channel activity",style="Field.TLabel").pack(side="left")
        ttk.Label(h,text="Packets  /  last heard (run time)",style="Small.TLabel").pack(side="right")
        self.channel_grid=ttk.Frame(self.channel_card)
        self.channel_grid.pack(fill="x",pady=(4,0))
        self.cells={}
        self.details(parent)
        for var in (self.id_filter,self.fc_filter,self.sf_filter):
            var.trace_add("write",self.filter_changed)
        self.sort_heading()

    @staticmethod
    def _combo(frame,label,var,width):
        ttk.Label(frame,text=label,style="Small.TLabel").pack(side="left")
        box=ttk.Combobox(frame,textvariable=var,width=width,state="readonly",values=("All",))
        box.pack(side="left",padx=(4,10))
        return box

    def details(self,parent):
        box=ttk.Frame(parent,style="Card.TFrame",padding=(14,10))
        box.grid(row=0,column=1,sticky="nsew")
        box.columnconfigure(0,weight=1)
        box.rowconfigure(3,weight=1)
        ttk.Label(box,text="SELECTED CHIPSAT",style="Overline.TLabel").grid(row=0,column=0,sticky="w")
        self.node_label=ttk.Label(box,text="No selection",style="Node.TLabel")
        self.node_label.grid(row=1,column=0,sticky="w")
        self.meta=ttk.Label(box,style="Small.TLabel")
        self.meta.grid(row=2,column=0,sticky="w",pady=(0,6))
        self.detail_book=ttk.Notebook(box,style="Details.TNotebook")
        self.detail_book.grid(row=3,column=0,sticky="nsew")
        tele=ttk.Frame(self.detail_book)
        tele.columnconfigure(0,weight=1)
        tele.rowconfigure(0,weight=1)
        canvas=tk.Canvas(tele,width=1,height=1,background=PANEL,highlightthickness=0,yscrollincrement=14)
        canvas.grid(row=0,column=0,sticky="nsew")
        scroll=ttk.Scrollbar(tele,orient="vertical",command=canvas.yview)
        scroll.grid(row=0,column=1,sticky="ns")
        def extent(first,last):
            scroll.set(first,last)
            if float(first)<=0 and float(last)>=1:
                scroll.grid_remove()
            else:
                scroll.grid()
        canvas.configure(yscrollcommand=extent)
        self.measure=ttk.Frame(canvas,padding=(3,4))
        item=canvas.create_window(0,0,anchor="nw",window=self.measure)
        canvas.bind("<Configure>",lambda e:canvas.itemconfigure(item,width=e.width))
        self.measure.bind("<Configure>",lambda _:canvas.configure(scrollregion=canvas.bbox("all")))
        history=ttk.Frame(self.detail_book,padding=(3,6))
        self.detail_book.add(tele,text="Telemetry")
        self.detail_book.add(history,text="History & payload")
        self.lines={}
        # The motion vectors keep the flight parser's XYZ and IJKR order.
        self._section(self.measure,0,"Position")
        self.fix=ttk.Label(self.measure,style="Accent.TLabel")
        self.fix.grid(row=1,column=0,columnspan=2,sticky="w")
        for row,(key,label) in enumerate((("coords","Lat / lon (°)"),("alt","Altitude MSL (m)")),2):
            self._line(self.measure,row,key,label)
        self._section(self.measure,4,"Power")
        self._line(self.measure,5,"soc","State of charge (%)")
        self.charge=ttk.Progressbar(self.measure,length=60,style="Charge.Horizontal.TProgressbar")
        self.charge.grid(row=5,column=1,sticky="w")
        self._section(self.measure,6,"Environment")
        self._line(self.measure,7,"temp","Temperature (°C)")
        self._line(self.measure,8,"pressure","Pressure (hPa)")
        self._line(self.measure,9,"humidity","Humidity (%)")
        self._line(self.measure,10,"env_alt","Altitude (m)")
        self._section(self.measure,11,"Motion")
        for row,(key,label) in enumerate((("acc","Accel. XYZ (m/s²)"),("gyro","Gyro XYZ (°/s)"),
                                         ("mag","Field XYZ (µT)"),("quat","Quaternion IJKR")),12):
            self._line(self.measure,row,key,label)
        def wheel(event):
            if float(canvas.yview()[1])-float(canvas.yview()[0])<1:
                canvas.yview_scroll(-int(event.delta/120),"units")
            return "break"
        for child in (canvas,self.measure,*self.measure.winfo_children()):
            child.bind("<MouseWheel>",wheel)
        ttk.Label(history,text="Latest raw payload (hex)",style="Field.TLabel").pack(anchor="w")
        self.hex_text=tk.Text(history,height=4,wrap="char",font=(self.mono,10),background="#f2f5f5",
                              foreground="#243c46",relief="flat",padx=7,pady=4,width=10)
        self.hex_text.pack(fill="x",pady=(4,10))
        self.hex_text.configure(state="disabled")
        ttk.Label(history,text="Recent packets",style="Field.TLabel").pack(anchor="w",pady=(0,5))
        frame=ttk.Frame(history)
        frame.pack(fill="both",expand=True)
        self.history=table(frame,(("time","Run time (s)",90),("seq","Counter",65),("fc","MHz",75),
                                  ("sf","SF",35),("phy","PHY",38),("app","App",38)),5)
        self.detail_key=None
        self.fix_key=None

    def _section(self,frame,row,label):
        header=ttk.Frame(frame)
        header.grid(row=row,column=0,columnspan=2,sticky="ew",pady=(3 if row else 0,1))
        ttk.Label(header,text=label,style="Field.TLabel").pack(side="left")
        ttk.Separator(header).pack(side="left",fill="x",expand=True,padx=(9,0))

    def _line(self,frame,row,key,label):
        frame.columnconfigure(1,weight=1)
        ttk.Label(frame,text=label,style="Small.TLabel").grid(row=row,column=0,sticky="w",padx=(0,6))
        self.lines[key]=ttk.Label(frame,text=DASH,style="Numeric.TLabel")
        self.lines[key].grid(row=row,column=1,sticky="e")

    def configure_channels(self,p):
        key=(tuple(p.carriers_hz),tuple(p.spreading_factors)) if p else ((),())
        if self.profile_key==key:
            return
        self.profile_key=key
        self.carriers,sfs=key
        self.fc_box.configure(values=("All",*(mhz_label(fc) for fc in self.carriers)))
        self.sf_box.configure(values=("All",*map(str,sfs)))
        for widget in self.channel_grid.winfo_children():
            widget.destroy()
        self.cells={}
        def cell(r,c,text,style):
            label=ttk.Label(self.channel_grid,text=text,style=style,anchor="center")
            label.grid(row=r,column=c,sticky="ew",padx=(0,2),pady=(0,1))
            return label
        cell(0,0,"Carrier (MHz)","GridHead.TLabel")
        for j,sf in enumerate(sfs,1):
            self.channel_grid.columnconfigure(j,weight=1,uniform="branch")
            cell(0,j,f"SF{sf}","GridHead.TLabel")
        for i,fc in enumerate(self.carriers,1):
            cell(i,0,mhz_label(fc),"GridHead.TLabel")
            for j,sf in enumerate(sfs,1):
                self.cells[fc,sf]=cell(i,j,f"0  /  {DASH}","GridIdle.TLabel")

    def filters(self):
        freq=next((fc for fc in self.carriers if mhz_label(fc)==self.fc_filter.get()),None)
        sf=self.sf_filter.get()
        return Filters(self.id_filter.get().strip(),freq,int(sf) if sf.isdecimal() else None)

    def clear_filters(self):
        self.id_filter.set("")
        self.fc_filter.set("All")
        self.sf_filter.set("All")

    def filter_changed(self,*_):
        if self.model is not None:
            self.render(self.model,self.now)

    def sort_by(self,key):
        self.reverse=not self.reverse if self.key==key else key in ("last","count","soc")
        self.key=key
        self.sort_heading()
        self.filter_changed()

    def sort_heading(self):
        for key,label,_ in self.columns:
            self.fleet.heading(key,text=label+(("  ▾" if self.reverse else "  ▴") if self.key==key else ""))

    def clear_log(self):
        self.log_floor=self.model.total if self.model else 0
        self.log.delete(*self.log.get_children())
        self.filter_changed()

    def copy_row(self):
        selected=self.log.selection()
        if selected and self.model:
            p=next((p for i,p in self.model.log if str(i)==selected[0]),None)
            if p:
                values=[*self.log.item(selected[0],"values"),p.row.get("receive_utc",""),p.row.get("payload_hex","")]
                self.log.clipboard_clear()
                self.log.clipboard_append("\t".join(map(str,values)))

    def reset(self):
        self.model=None
        self.selected_id=None
        self.log_floor=0
        self.detail_key=self.fix_key=None
        self.pause.set(False)
        self.key,self.reverse="id",False
        self.sort_heading()
        self.clear_filters()
        for tree in (self.fleet,self.log,self.history):
            tree.delete(*tree.get_children())
        self.detail_book.select(0)

    def select(self,*_):
        selection=self.fleet.selection()
        if selection and self.model and int(selection[0]) in self.model.nodes:
            self.selected_id=int(selection[0])
            self.show_details(self.model.nodes[self.selected_id])

    def render(self,model,now):
        self.model,self.now=model,now
        filters=self.filters()
        ids=model.visible(filters,self.key,self.reverse)
        self.totals.configure(text=f"{len(ids)} of {len(model.nodes)} ChipSats   ·   {model.total:,} packets")
        present=set(self.fleet.get_children())
        wanted=set(map(str,ids))
        if present-wanted:
            self.fleet.delete(*(present-wanted))
        for index,i in enumerate(ids):
            n=model.nodes[i]
            p=n.latest
            age=max(0,now-p.t)
            soc=f"{n.power.data['state_of_charge_percent']:g}" if n.power else DASH
            values=(f"{i:03}",clock_text(p.t,True),f"{age:.1f}",n.count,mhz_label(p.fc),p.sf,soc)
            tags=["alternate"] if index%2 else []
            if age>=STALE_SECONDS:
                tags.append("stale")
            kw=dict(values=values,tags=tags)
            if str(i) in present:
                self.fleet.item(str(i),**kw)
                if self.fleet.index(str(i))!=index:
                    self.fleet.move(str(i),"",index)
            else:
                self.fleet.insert("",index,iid=str(i),**kw)
        selected=self.selected_id if self.selected_id in ids else (ids[0] if ids else None)
        if selected is not None:
            self.selected_id=selected
            if self.fleet.selection()!=(str(selected),):
                self.fleet.selection_set(str(selected))
            self.empty.place_forget()
        else:
            self.empty.configure(text="No ChipSats match these filters" if model.nodes else "Waiting for a valid ChipSat packet")
            self.empty.place(relx=.5,rely=.55,anchor="center")
        self.show_details(model.nodes.get(selected))
        for key,cell in self.cells.items():
            count,last=model.channels.get(key,(0,None))
            stamp=clock_text(last,True) if last is not None else DASH
            cell.configure(text=f"{count}  /  {stamp}",style="GridCell.TLabel" if count else "GridIdle.TLabel")
        log=[(i,p) for i,p in model.log if i>self.log_floor and filters.matches(p)]
        old=set(self.log.get_children())
        obsolete=old-{str(i) for i,_ in log}
        if obsolete:
            self.log.delete(*obsolete)
        added=False
        for index,(i,p) in enumerate(log):
            if str(i) not in old:
                self.log.insert("",index,iid=str(i),values=(i,f"{p.t:.3f}",p.node if p.node is not None else DASH,
                    mhz_label(p.fc),p.sf,crc_text(p.row["phy_crc_ok"]),crc_text(p.row["application_crc_ok"]),
                    p.row.get("packet_counter") or (0 if p.row.get("packet_counter")==0 else DASH)),
                    tags=("error",) if False in (crc(p.row["phy_crc_ok"]),crc(p.row["application_crc_ok"])) else ())
                added=True
            elif self.log.index(str(i))!=index:
                self.log.move(str(i),"",index)
        if added and not self.pause.get():
            self.log.yview_moveto(1)
        self.log_note.configure(text=f"{len(log)} displayed   ·   {model.total:,} logged   ·   View limit {LOG_LIMIT}")

    def show_details(self,n):
        last=n.latest if n else None
        pos=n.position if n else None
        fixed=bool(last and last.data.get("valid",{}).get("gps"))
        fix_age=clock_text(self.now-pos.t,True) if pos else None
        fix_text=(f"Valid fix   ·   age {fix_age}" if fixed else
                  f"No fix   ·   last valid position {fix_age} ago" if pos else "No valid position")
        if not n:
            fix_text=DASH
        if self.fix_key!=fix_text:
            stale=last and (not fixed or (pos and self.now-pos.t>=STALE_SECONDS))
            self.fix.configure(text=fix_text,style="Stale.TLabel" if stale else "Accent.TLabel")
            self.fix_key=fix_text
        identity=(id(last),n.count if n else None)
        if self.detail_key==identity:
            return
        self.detail_key=identity
        self.node_label.configure(text=f"ChipSat {last.node:03d}" if n else "No selection")
        self.meta.configure(text=(f"Packet {last.row['packet_counter']}   ·   {mhz_label(last.fc)} MHz   ·   SF{last.sf}"
                                  if n else "Select a fleet row to inspect its telemetry"))
        vals={}
        if pos:
            d=pos.data
            vals.update(coords=f"{d['latitude_deg']:.7f}, {d['longitude_deg']:.7f}",alt=f"{d['gps_altitude_msl_m']:.3f}")
        if last:
            d=last.data
            valid=d["valid"]
            if valid["state_of_charge"]:
                vals["soc"]=f"{d['state_of_charge_percent']:g}"
            if valid["environmental"]:
                for key,src in (("temp","temperature_C"),("pressure","pressure_hPa"),("humidity","humidity_percent"),
                                ("env_alt","environmental_altitude_m")):
                    vals[key]=f"{d[src]:.2f}"
            for key,group,src in (("acc","linear_acceleration","linear_acceleration_mps2"),
                                  ("gyro","gyroscope","gyroscope_deg_s"),("mag","magnetometer","magnetic_field_uT"),
                                  ("quat","quaternion","quaternion_ijkr")):
                if valid[group]:
                    vals[key]="  ".join(f"{v:.{3 if key=='quat' else 2}f}" for v in d[src])
        for key,label in self.lines.items():
            label.configure(text=vals.get(key,DASH))
        if "soc" in vals:
            self.charge.configure(value=float(vals["soc"]))
            self.charge.grid()
        else:
            self.charge.grid_remove()
        self.hex_text.configure(state="normal")
        self.hex_text.delete("1.0","end")
        self.hex_text.insert("1.0",bytes.fromhex(last.row["payload_hex"]).hex(" ").upper() if last else DASH)
        self.hex_text.configure(state="disabled")
        self.history.delete(*self.history.get_children())
        if n:
            for p in n.history:
                self.history.insert("","end",values=(f"{p.t:.3f}",p.row["packet_counter"],mhz_label(p.fc),p.sf,
                    crc_text(p.row["phy_crc_ok"]),crc_text(p.row["application_crc_ok"])))

