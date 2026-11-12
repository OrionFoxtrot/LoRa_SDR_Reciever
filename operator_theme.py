"""Native Tk styling and the configured capture-band display."""
import tkinter.font as tkfont
from tkinter import ttk

PAPER = "#edf2f3"
PANEL = "#ffffff"
NAVY = "#132f38"
INK = "#17343e"
MUTED = "#45616a"
EDGE = "#cbd7d9"
TEAL = "#006d60"
MINT = "#cdf3e8"
RED = "#a73525"


def configure(root):
    installed = set(tkfont.families(root))
    family = next((f for f in ("Inter", "Roboto", "Segoe UI") if f in installed), "Arial")
    mono = "Consolas" if "Consolas" in installed else "Courier"
    tkfont.nametofont("TkDefaultFont",root=root).configure(family=family,size=10)
    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", font=(family, 10), background=PANEL, foreground=INK,
                bordercolor=EDGE, lightcolor=EDGE, darkcolor=EDGE, focuscolor=TEAL)
    s.configure("TFrame", background=PANEL)
    s.configure("Page.TFrame", background=PAPER)
    s.configure("Header.TFrame", background=NAVY)
    s.configure("Card.TFrame", background=PANEL, borderwidth=1, relief="solid")
    s.configure("TLabel", background=PANEL)
    s.configure("Small.TLabel", font=(family, 9), foreground=MUTED)
    s.configure("Field.TLabel", font=(family, 9, "bold"))
    s.configure("Heading.TLabel", font=(family, 14, "bold"))
    s.configure("Metric.TLabel", font=(mono, 17, "bold"))
    s.configure("Warn.Metric.TLabel", foreground=RED)
    s.configure("Title.TLabel", background=NAVY, foreground="#f6fbfa", font=(family, 17, "bold"))
    s.configure("Brand.TLabel", background=NAVY, foreground="#9edacd", font=(family, 9, "bold"))
    s.configure("State.TLabel", background="#244851", foreground="#d5fff0", font=(family, 10, "bold"),
                padding=(12, 5))
    s.configure("Error.State.TLabel", background="#66372e", foreground="#ffe7dd")
    s.configure("TButton", background="#e7efee", borderwidth=1, padding=(9, 5))
    s.map("TButton", background=[("active","#dae8e5")], foreground=[("disabled","#70828a")])
    s.configure("Start.TButton", background=TEAL, foreground="white", font=(family, 10, "bold"),
                padding=(14, 7), borderwidth=0)
    s.map("Start.TButton", background=[("disabled","#344e56"),("active","#005a50")],
          foreground=[("disabled","#bccdce"),("!disabled","white")])
    s.configure("Stop.TButton", background="#29454e", foreground="white", padding=(12, 7), borderwidth=0)
    s.map("Stop.TButton", background=[("disabled","#233e47"),("active","#375a63")],
          foreground=[("disabled","#96acb0"),("!disabled","white")])
    s.configure("TEntry", fieldbackground="white", padding=(6,4), insertcolor=INK)
    s.map("TEntry",fieldbackground=[("disabled","#f0f3f3")],foreground=[("disabled","#697e83")])
    for base in ("TCheckbutton", "TRadiobutton"):
        s.configure(base, background=PANEL, padding=(2,3))
        s.map(base, background=[("active","#e4f2ed")])
    for name in ("Choice.TCheckbutton","Choice.TRadiobutton"):
        s.layout(name,s.layout("Toolbutton"))
        s.configure(name, background="#e9eef0", foreground=MUTED, padding=(7,5),
                    borderwidth=1, relief="flat", font=(family,9,"bold"))
        s.map(name, background=[("selected",MINT),("active","#dce9e6")],
              foreground=[("selected","#005549"),("disabled","#6f8184")],
              bordercolor=[("selected","#63aa98")], relief=[("selected","solid")])
    s.configure("TNotebook", borderwidth=0,tabmargins=(0,0,0,3),background=PANEL)
    s.configure("TNotebook.Tab", padding=(12,6), background="#e8eef0",font=(family,10,"bold"))
    s.map("TNotebook.Tab", background=[("selected",NAVY)],foreground=[("selected","white")])
    s.configure("Treeview", rowheight=max(26,tkfont.Font(root=root,family=family,size=10).metrics("linespace")+8),
                font=(mono,10),fieldbackground=PANEL, borderwidth=0)
    s.configure("Treeview.Heading", background="#eaf0f1",font=(family,9,"bold"),
                foreground=MUTED, padding=(3,5), relief="flat")
    s.map("Treeview",background=[("selected","#d6ece6")],foreground=[("selected",INK)])
    s.configure("Overline.TLabel", foreground=MUTED, font=(family,9,"bold"))
    s.configure("Node.TLabel", foreground=INK, font=(family,21,"bold"))
    s.configure("Numeric.TLabel", font=(mono,10), foreground=INK)
    s.configure("Accent.TLabel", foreground=TEAL, font=(family,10,"bold"))
    s.configure("Alert.TLabel", foreground=RED, font=(family,10,"bold"))
    s.configure("Stale.TLabel", foreground="#935b16", font=(family,9,"bold"))
    s.configure("HeaderValue.TLabel",background=NAVY,foreground="#f4f8f8",font=(mono,13,"bold"))
    s.configure("HeaderMeta.TLabel",background=NAVY,foreground="#bccdd1",font=(family,9))
    s.configure("Recording.TLabel",background=NAVY,foreground="#ffac98",font=(family,10,"bold"))
    s.configure("HeaderState.TLabel",background=NAVY,foreground="#e8f1f2",font=(family,10,"bold"))
    s.configure("Foot.TLabel",background=PAPER,foreground=MUTED,font=(family,9))
    s.configure("GridHead.TLabel",background="#eaf0f1",foreground=MUTED,font=(family,9,"bold"),padding=(7,2))
    s.configure("GridCell.TLabel",background="#f4f7f7",foreground=INK,font=(mono,9),padding=(7,2))
    s.configure("GridIdle.TLabel",background="#f4f7f7",foreground=MUTED,font=(mono,9),padding=(7,2))
    s.configure("Details.TNotebook.Tab",padding=(8,4),font=(family,9))
    s.configure("TCombobox",padding=(3,3),fieldbackground="white")
    s.configure("Charge.Horizontal.TProgressbar", background="#638984", troughcolor="#e1eae9",
                borderwidth=0, thickness=5)
    s.map("TCombobox",fieldbackground=[("readonly","white")],foreground=[("readonly",INK)])
    return family,mono


def mhz_label(hz):
    return f"{hz/1e6:.3f}" if hz % 1000 == 0 else f"{hz/1e6:.6f}".rstrip("0")


def draw_band(canvas, center, fs, bw, carriers, mono):
    """Draw selected RF bandwidths on the actual sampled span."""
    canvas.delete("all")
    width = canvas.winfo_width()
    if width < 80:
        return
    left, right = 12, width-12
    low = center-fs/2
    def x(hz):
        return left+(hz-low)*(right-left)/fs
    canvas.create_rectangle(left,21,right,34,fill="#263f48",outline="")
    for fc in carriers:
        a,b = max(left,x(fc-bw/2)),min(right,x(fc+bw/2))
        if a < b:
            canvas.create_rectangle(a,21,b,34,fill="#66cfaf",outline="#153e40")
            canvas.create_line(x(fc),18,x(fc),37,fill="#e9fff7")
    canvas.create_line(x(center),13,x(center),37,fill="#e4f3ef",dash=(2,2))
    font=(mono,9)
    canvas.create_text(left,45,text=mhz_label(round(low)),fill="#c2d4d5",anchor="w",font=font)
    canvas.create_text(right,45,text=mhz_label(round(low+fs)),fill="#c2d4d5",anchor="e",font=font)
    canvas.create_text(x(center),8,text=f"{mhz_label(center)} MHz",fill="#c2ffe9",font=font)
