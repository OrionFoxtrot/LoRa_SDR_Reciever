"""Capture the populated Windows operator client after replaying a real IQ file.

QA tool only. Requires Pillow. A private desktop is optional and is never activated.
"""
import argparse
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import sys
import time


def private_desktop():
    user=ctypes.WinDLL("user32",use_last_error=True)
    user.CreateDesktopW.argtypes=[wintypes.LPCWSTR,wintypes.LPCWSTR,wintypes.LPVOID,
                                  wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID]
    user.CreateDesktopW.restype=wintypes.HANDLE
    user.SetThreadDesktop.argtypes=[wintypes.HANDLE]
    handle=user.CreateDesktopW(f"DeSCENT-preview-{os.getpid()}",None,None,0,0x1ff,None)
    if not handle or not user.SetThreadDesktop(handle):
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("iq",type=Path)
    parser.add_argument("png",type=Path)
    parser.add_argument("--out",type=Path,required=True)
    parser.add_argument("--name",default="Fleet bench replay")
    parser.add_argument("--private-desktop",action="store_true")
    args=parser.parse_args()
    if args.private_desktop:
        private_desktop()
    import tkinter as tk
    from PIL import ImageGrab
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from operator_gui import ReceiverWindow
    root=tk.Tk()
    root.withdraw()
    try:
        window=ReceiverWindow(root,"Replay",str(args.iq.resolve()))
        window.setup.output.set(str(args.out.resolve()))
        window.setup.paced.set(False)
        window.name.set(args.name)
        window.start()
        while window.worker:
            root.update()
            time.sleep(.02)
        if window.phase!="saved" or not window.model.nodes:
            raise RuntimeError(window.detail.cget("text"))
        window.panels.sort_by("last")
        window.panels.fleet.yview_moveto(0)
        first=window.panels.fleet.get_children()[0]
        window.panels.fleet.selection_set(first)
        window.panels.select()
        root.overrideredirect(True)
        root.geometry("1366x768+16+16")
        root.deiconify()
        root.lift()
        for _ in range(12):
            root.update()
            time.sleep(.04)
        # Capture exclusively our Tk client. No desktop or other applications are read.
        image=ImageGrab.grab(window=root.winfo_id())
        if image.size!=(1366,768) or len(image.resize((160,90)).getcolors(14400))<16:
            raise RuntimeError("Windows did not render a 1366 × 768 receiver client.")
        args.png.parent.mkdir(exist_ok=True,parents=True)
        image.save(args.png)
        print(f"{args.png}: {image.size}; {window.model.total} packets, {len(window.model.nodes)} ChipSats")
        print("Replay run",window.path)
    finally:
        root.destroy()


if __name__=="__main__":
    main()

