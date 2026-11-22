"""Save Windows client screenshots from actual journals on an isolated QA desktop."""
import argparse
from pathlib import Path
import sys
import time
from capture_operator import private_desktop


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("fleet","bench","recorded","out"):
        parser.add_argument("--"+name,type=Path,required=True)
    parser.add_argument("--private-desktop",action="store_true")
    args=parser.parse_args()
    if args.private_desktop:
        private_desktop()
    import tkinter as tk
    from PIL import ImageGrab
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from operator_gui import ReceiverWindow
    from mission import Mission
    r=tk.Tk()
    r.withdraw()
    u=ReceiverWindow(r)
    r.overrideredirect(True)
    r.geometry("1366x768+12+12")
    r.deiconify()
    args.out.mkdir(parents=True,exist_ok=True)
    def load(path,m=None):
        u.field.open_saved(path,mission=m)
        while u.phase=="loading":
            r.update()
            time.sleep(.015)
        u.views.select(0)
        u.panels.detail_book.select(0)
    def select(node):
        u.panels.fleet.selection_set(str(node))
        u.panels.fleet.see(str(node))
        u.panels.select()
    def save(name):
        for _ in range(15):
            r.update()
            time.sleep(.04)
        img=ImageGrab.grab(window=r.winfo_id())
        if img.size!=(1366,768) or len(img.resize((160,90)).getcolors(14400))<16:
            raise RuntimeError("Missing Windows client rendering")
        img.save(args.out/name)
        print(name,u.model.total,"packets,",u.heard_count.cget("text"),"heard,",
              u.valid_count.cget("text"),"reporting valid data",flush=True)
    try:
        load(args.fleet)
        u.panels.sort_by("age")
        select(int(u.panels.fleet.get_children()[0]))
        save("field-fleet.png")
        load(args.recorded,Mission(size=2,target=2))
        select(249)
        save("field-bench.png")
        plots=u.panels.plots
        u.panels.detail_book.select(plots.trend)
        r.update()
        plots.refresh(u.model,249,u.snapshot["capture_s"],force=True)
        pt=plots.picks["trend"][len(plots.picks["trend"])//2]
        plots.click("trend",pt[0],pt[1])
        save("field-trends.png")
        load(args.bench)
        select(249)
        plots.context.set(True)
        u.panels.detail_book.select(plots.track)
        r.update()
        plots.refresh(u.model,249,u.snapshot["capture_s"],force=True)
        save("field-track.png")
        u.views.select(u.generic_page)
        u.panels.sf_filter.set("10")
        u.generic.render(u.model)
        u.generic.list.selection_set(0)
        save("field-generic.png")
    finally:
        u.close()


if __name__=="__main__":
    main()
