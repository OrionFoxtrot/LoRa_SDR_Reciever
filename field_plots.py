"""Offline Tk scatter plots on capture time and a shared WGS84 ground plane."""
import math
import time
import tkinter as tk
from tkinter import ttk

from mission import fix_time
from observations import SERIES, east_north
from operator_theme import mhz_label

INK="#203b47"
MUTED="#506670"
GRID="#d7e0e4"
BLUE="#215d9d"
TEAL="#007669"
AMBER="#986225"


class FieldPlots:
    def __init__(self,book,history_tab):
        self.book=book
        self.trend=ttk.Frame(book,padding=(4,4))
        self.track=ttk.Frame(book,padding=(4,4))
        book.insert(history_tab,self.trend,text="Trends")
        book.insert(history_tab,self.track,text="Ground track")
        self.metric=tk.StringVar(value="Preamble power")
        self.window=tk.StringVar(value="Last 60 s")
        self.follow=tk.BooleanVar(value=True)
        self.context=tk.BooleanVar()
        self.charts={}
        self.notes={}
        self.captions={}
        self.packet_buttons={}
        for frame,key in ((self.trend,"trend"),(self.track,"track")):
            tools=ttk.Frame(frame)
            tools.pack(fill="x")
            if key=="trend":
                metrics=ttk.Combobox(tools,textvariable=self.metric,values=list(SERIES),state="readonly",width=19)
                metrics.pack(side="left",fill="x",expand=True)
                span=ttk.Combobox(tools,textvariable=self.window,values=("Last 60 s","Last 5 min","Full run"),state="readonly",width=10)
                span.pack(side="left",padx=(4,0))
                span.bind("<<ComboboxSelected>>",lambda _:self.resume())
                metrics.bind("<<ComboboxSelected>>",lambda _:self.invalidate())
                nav=ttk.Frame(frame)
                nav.pack(fill="x")
                ttk.Checkbutton(nav,text="Follow latest",variable=self.follow,command=self.toggle_follow).pack(side="left")
                for label,factor in (("−",1.7),("+",1/1.7)):
                    ttk.Button(nav,text=label,width=2,command=lambda f=factor:self.zoom(f)).pack(side="right")
            else:
                ttk.Checkbutton(tools,text="Show fleet context",variable=self.context,command=self.invalidate).pack(side="left")
            caption=ttk.Label(frame,style="Small.TLabel",wraplength=400)
            caption.pack(anchor="w")
            self.captions[key]=caption
            c=tk.Canvas(frame,bg="#f9fbfb",highlightthickness=0,width=100,height=90)
            c.pack(fill="both",expand=True,pady=3)
            self.charts[key]=c
            c.bind("<Configure>",lambda _:self.invalidate())
            c.bind("<Button-1>",lambda e,k=key:self.click(k,e.x,e.y))
            note=ttk.Label(frame,style="Small.TLabel",wraplength=405)
            note.pack(anchor="w",fill="x")
            self.notes[key]=note
            button=ttk.Button(frame,text="Inspect selected packet",command=lambda k=key:self.packet_detail(k))
            self.packet_buttons[key]=button
        self.charts["trend"].bind("<MouseWheel>",lambda e:self.zoom(1/1.5 if e.delta>0 else 1.5,e.x))
        self.charts["trend"].bind("<B1-Motion>",self.drag)
        self.book.bind("<<NotebookTabChanged>>",lambda _:self.invalidate(),add="+")
        self.reset()

    def reset(self):
        self.bounds=None
        self.follow.set(True)
        self.context.set(False)
        self.window.set("Last 60 s")
        self.metric.set("Preamble power")
        self.signature=None
        self.next_draw=0
        self.picks={"trend":[],"track":[]}
        self.selected={}
        self.picked_rows={}
        self.domain=(0.,60.)
        self.drag_at=None
        self.node=None
        self.origin_key=None
        self.track_extent=(2.5,2.5)
        self.extent_packets={}
        for key,c in self.charts.items():
            c.delete("all")
            self.notes[key].configure(text="")
            self.packet_buttons[key].pack_forget()
            self.captions[key].configure(text="")

    def invalidate(self):
        self.signature=None

    def resume(self):
        self.follow.set(True)
        self.bounds=None
        self.invalidate()

    def toggle_follow(self):
        self.bounds=None if self.follow.get() else self.domain
        self.invalidate()

    def zoom(self,factor,x=None):
        a,b=self.domain
        u=.5 if x is None else max(0,min(1,(x-55)/max(1,self.charts["trend"].winfo_width()-69)))
        center=a+(b-a)*u
        span=max(.5,(b-a)*factor)
        left=max(0,center-u*span)
        self.follow.set(False)
        self.bounds=(left,left+span)
        self.invalidate()

    def drag(self,e):
        if self.drag_at is None:
            return
        x,a,b=self.drag_at
        if abs(e.x-x)<4:
            return
        lo=max(0,a+(x-e.x)*(b-a)/max(1,self.charts["trend"].winfo_width()-69))
        self.follow.set(False)
        self.bounds=(lo,lo+b-a)
        self.invalidate()

    def click(self,key,x,y):
        if key=="trend":
            self.drag_at=(x,*self.domain)
        found=min(self.picks[key],key=lambda pt:(x-pt[0])**2+(y-pt[1])**2,default=None)
        if found and math.hypot(x-found[0],y-found[1])<=13:
            _,_,r,value=found
            self.selected[key]=(r["rowid"],value)
            self.picked_rows[key]=r
            self.packet_buttons[key].pack(anchor="w")
            if key=="trend":
                self.follow.set(False)
                self.bounds=self.domain
            self.notes[key].configure(text=f"{r['t']:.3f} s  ·  {value}  ·  packet {r['seq']}  ·  {mhz_label(r['fc'])} MHz  ·  SF{r['sf']}  ·  {r['pass']}")
            self.invalidate()

    def packet_detail(self,key):
        r=self.picked_rows.get(key)
        if not r:
            return
        top=tk.Toplevel(self.book)
        top.title(f"ChipSat {r['n']} · packet {r['seq']}")
        box=ttk.Frame(top,padding=12)
        box.pack()
        ttk.Label(box,text=f"{r['t']:.6f} s   ·   {mhz_label(r['fc'])} MHz   ·   SF{r['sf']}   ·   {r['pass']}").pack(anchor="w")
        ttk.Label(box,text=f"PHY CRC pass   ·   Application CRC pass   ·   {r['utc'] or 'UTC unavailable'}").pack(anchor="w")
        raw=bytes.fromhex(r["raw"]).hex(" ").upper()
        text=tk.Text(box,height=5,width=50,wrap="word")
        text.pack(pady=5)
        text.insert("1.0",raw)
        text.configure(state="disabled")
        def copy():
            top.clipboard_clear()
            top.clipboard_append(raw)
        ttk.Button(box,text="Copy payload",command=copy).pack(anchor="e")

    def refresh(self,model,node,now,*,force=False,gaps=()):
        tab=self.book.select()
        key="trend" if tab==str(self.trend) else "track" if tab==str(self.track) else None
        if not key or (not force and not self.charts[key].winfo_ismapped()):
            return
        if self.node!=node:
            self.selected.clear()
            self.picked_rows.clear()
            for button in self.packet_buttons.values():
                button.pack_forget()
            self.node=node
            for note in self.notes.values():
                note.configure(text="")
        c=self.charts[key]
        sig=(key,id(model),node,model.total,round(now,1) if self.follow.get() or key=="track" else 0,
             self.metric.get(),self.window.get(),self.bounds,self.context.get(),c.winfo_width(),c.winfo_height())
        if not force and (sig==self.signature or time.monotonic()<self.next_draw):
            return
        self.signature=sig
        self.next_draw=time.monotonic()+.4
        self.picks[key]=[]
        c.delete("all")
        w,h=c.winfo_width(),c.winfo_height()
        if w<80 or h<60:
            return
        if node is None or node not in model.nodes or not model.observations:
            self.captions[key].configure(text="")
            c.create_text(w/2,h/2,text="Waiting for first valid GPS fix" if key=="track" else "Select a ChipSat",fill=MUTED)
            return
        if key=="trend":
            self.trend_draw(model,node,now,w,h,gaps)
        else:
            self.track_draw(model,node,now,w,h)

    def axes(self,c,w,h,xr,yr,xlabel,ylabel):
        l,r,t,b=53,w-14,23,h-34
        def x(v):return l+(r-l)*(v-xr[0])/(xr[1]-xr[0])
        def y(v):return b-(b-t)*(v-yr[0])/(yr[1]-yr[0])
        for i in range(5):
            at=i/4
            yy=b-at*(b-t)
            xx=l+at*(r-l)
            c.create_line(l,yy,r,yy,fill=GRID)
            c.create_line(xx,t,xx,b,fill=GRID)
            c.create_text(l-4,yy,anchor="e",text=f"{yr[0]+at*(yr[1]-yr[0]):.4g}",fill=MUTED,font=("Segoe UI",8))
            c.create_text(xx,b+3,anchor="n",text=f"{xr[0]+at*(xr[1]-xr[0]):.4g}",fill=MUTED,font=("Segoe UI",8))
        c.create_text(l,7,anchor="w",text=ylabel,fill=INK,font=("Segoe UI",9,"bold"))
        c.create_text((l+r)/2,h-3,anchor="s",text=xlabel,fill=INK,font=("Segoe UI",8))
        return x,y

    def dot(self,key,c,x,y,r,value,color):
        select=self.selected.get(key)==(r["rowid"],value)
        radius=5 if select else 3
        c.create_oval(x-radius,y-radius,x+radius,y+radius,fill=color,outline=INK if select else "white")
        self.picks[key].append((x,y,r,value))

    def trend_draw(self,model,node,now,w,h,gaps):
        c=self.charts["trend"]
        span={"Last 60 s":60.,"Last 5 min":300.,"Full run":max(now,1.)}[self.window.get()]
        lo,hi=self.bounds or (max(0.,now-span),max(1.,now))
        self.domain=(lo,hi)
        rows,sampled=model.observations.series(node,lo,hi)
        metric,unit=SERIES[self.metric.get()]
        cols=(("gps","m MSL",TEAL),("baro","m barometric",BLUE)) if metric=="both" else ((metric,unit,TEAL),)
        vals=[r[col] for r in rows for col,_,_ in cols if r[col] is not None]
        caption="GPS MSL (green)   ·   Barometric (blue)" if metric=="both" else ""
        if sampled:
            caption=(caption+"   ·   " if caption else "")+"Sampled observations"
        self.captions["trend"].configure(text=caption)
        if caption:
            self.captions["trend"].pack(before=c,anchor="w")
        else:
            self.captions["trend"].pack_forget()
        if not vals:
            c.create_text(w/2,h/2,text="No valid observations in this interval",fill=MUTED)
            return
        pad=max((max(vals)-min(vals))*.12,.1)
        x,y=self.axes(c,w,h,(lo,hi),(min(vals)-pad,max(vals)+pad),"Capture time (s)",unit)
        for a,b in gaps:
            if a<hi and b>lo:
                c.create_rectangle(x(max(lo,a)),23,x(min(hi,b)),h-34,fill="#f4e9d9",outline="",stipple="gray50")
        for col,units,color in cols:
            prev=None
            for r in rows:
                v=r[col]
                if v is None:
                    prev=None
                    continue
                xx,yy=x(r["t"]),y(v)
                if prev and not sampled and r["t"]-prev[0]<=1.5*model.book.m.period and not any(a<r["t"] and b>prev[0] for a,b in gaps):
                    c.create_line(prev[1],prev[2],xx,yy,fill=color)
                self.dot("trend",c,xx,yy,r,f"{v:.3f} {units}",color)
                prev=(r["t"],xx,yy)
        if not self.selected.get("trend"):
            self.notes["trend"].configure(text="Select a point for packet details. Drag to pan or use + / − to zoom.")

    def track_draw(self,model,node,now,w,h):
        c=self.charts["track"]
        first=model.first_fix
        station=model.book.m.station
        origin=station or ((first.data["latitude_deg"],first.data["longitude_deg"]) if first else None)
        rows,sampled=model.observations.series(node,0,now,fixes=True)
        self.captions["track"].configure(text=(("Station origin" if station else "First run fix origin")+
                         f"   {origin[0]:.5f}°, {origin[1]:.5f}°") if origin else "")
        if not rows or not origin:
            c.create_text(w/2,h/2,text="Waiting for first valid GPS fix",fill=MUTED)
            self.notes["track"].configure(text="")
            return
        report_count=len(rows)
        fixes={}
        for r in rows:
            ident=(r["fix_t"],r["lat"],r["lon"]) if r["dated"] else (r["rowid"],)
            fixes.setdefault(ident,r)
        rows=sorted(fixes.values(),key=lambda r:(r["fix_t"],r["t"]))
        points=[east_north(origin,r["lat"],r["lon"]) for r in rows]
        # Use one station-centred extent for all IDs. Visiting another tab or ID must
        # not move the coordinate grid or magnify a stationary radio's single fix.
        if self.origin_key!=origin:
            self.origin_key=origin
            self.track_extent=(2.5,2.5)
            self.extent_packets.clear()
        for i,n in model.nodes.items():
            pos=n.position
            if pos and self.extent_packets.get(i)!=pos.t:
                self.extent_packets[i]=pos.t
                e,north=east_north(origin,pos.data["latitude_deg"],pos.data["longitude_deg"])
                self.track_extent=max(self.track_extent[0],abs(e)),max(self.track_extent[1],abs(north))
        self.track_extent=(max(self.track_extent[0],max(abs(e) for e,_ in points)),
                           max(self.track_extent[1],max(abs(n) for _,n in points)))
        context=[]
        if self.context.get():
            for i,n in model.nodes.items():
                if i!=node and n.position:
                    d=n.position.data
                    context.append(east_north(origin,d["latitude_deg"],d["longitude_deg"]))
        wplot,hplot=max(1,w-67),max(1,h-57)
        scale=2*max(self.track_extent[0]/wplot,self.track_extent[1]/hplot)*1.12
        x,y=self.axes(c,w,h,(-wplot*scale/2,wplot*scale/2),(-hplot*scale/2,hplot*scale/2),"East (m)","North (m)")
        xx,yy=x(0),y(0)
        c.create_line(xx-4,yy,xx+4,yy,fill=MUTED)
        c.create_line(xx,yy-4,xx,yy+4,fill=MUTED)
        for e,n in context:
            xx,yy=x(e),y(n)
            c.create_oval(xx-2,yy-2,xx+2,yy+2,fill="#acb9bf",outline="")
        previous=None
        prior_time=None
        for j,(r,(e,n)) in enumerate(zip(rows,points)):
            xx,yy=x(e),y(n)
            if previous and (xx,yy)!=previous and not model.observations.lost_fix_between(node,prior_time,r["t"]):
                c.create_line(*previous,xx,yy,fill=TEAL,width=2,arrow="last" if j==len(rows)-1 or j==len(rows)//2 else "none")
            self.dot("track",c,xx,yy,r,f"{e:.1f} E, {n:.1f} N (m)",TEAL)
            previous=(xx,yy)
            prior_time=r["t"]
        stationary=all(math.dist(points[0],pt)<.01 for pt in points)
        marks=[(0,"First / latest")] if stationary else [(0,"First"),(-1,"Latest")]
        for j,label in marks:
            xx,yy=x(points[j][0]),y(points[j][1])
            c.create_oval(xx-4,yy-4,xx+4,yy+4,outline=BLUE if j==0 else TEAL,width=2)
            c.create_text(min(w-39,max(64,xx)),yy-6,text=label,anchor="s",fill=INK,font=("Segoe UI",8,"bold"))
        pos=model.nodes[node].position
        t,dated=fix_time(pos)
        d=pos.data
        self.captions["track"].configure(text=self.captions["track"].cget("text")+
            f"\n{d['latitude_deg']:.6f}°, {d['longitude_deg']:.6f}°   ·   {d['gps_altitude_msl_m']:g} m MSL"+
            f"\n{'Fix age' if dated else 'Last valid report'} {max(0,now-t):.1f} s"+("" if dated else " ago"))
        if not self.selected.get("track"):
            self.notes["track"].configure(text=("Sampled track" if sampled else f"{report_count} valid reports")+
                                          "   ·   Select a point for packet details.")
