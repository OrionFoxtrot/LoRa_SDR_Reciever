import binascii
import csv
import gc
import json
from pathlib import Path
import struct
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch
import weakref

from config import ROOT
from fixtures import GOLD, payload
from fleet_view import Fleet, Filters, HISTORY_LIMIT, LOG_LIMIT, export_csv
from operator_gui import ReceiverWindow
from reports import telemetry, write_packets
from run import live


def read_rows(path):
    with path.open(encoding="utf-8-sig",newline="") as stream:
        return list(csv.DictReader(stream))


def row(data,t):
    fields=dict(start_sample=round(t*1e6),capture_s=t,carrier_hz=915000000,sf=11,
                phy_crc_ok=True,payload_hex=data.hex(),run_id="unit",run_name="Unit",receive_utc="test")
    return telemetry(fields,"primary")


class FleetDataTests(unittest.TestCase):
    def test_no_fix_zero_sort_and_bounded_display(self):
        good=bytearray(payload(2,0)[:-2])
        good[:8]=bytes(8)                         # A valid position on the equator and prime meridian.
        good[51]=0                                # A measured zero SoC.
        def frame(b):
            return bytes(b)+binascii.crc_hqx(b,0xffff).to_bytes(2,"little")
        valid_packet=frame(good)
        good[52]&=~0x30
        invalid_packet=frame(good)
        rows=[row(valid_packet,0),row(payload(100,0),2),row(payload(10,0),1)]
        rows.extend(row(invalid_packet,3+i) for i in range(LOG_LIMIT+5))
        rows.append(row(valid_packet,.5))         # A later decoder completion carrying an older RF time.
        rows.append(dict(row(GOLD[1],500),phy_crc_ok=False))
        model=Fleet()
        for r in rows:
            model.ingest(r)
        node=model.nodes[2]
        self.assertEqual(len(model.log),LOG_LIMIT)
        self.assertEqual(len(node.history),HISTORY_LIMIT)
        self.assertFalse(node.latest.data["valid"]["gps"])
        self.assertEqual((node.position.t,node.power.t),(.5,.5))
        self.assertEqual(node.power.data["state_of_charge_percent"],0.)
        self.assertEqual(model.visible(Filters(),"id",False),[2,10,100])
        self.assertEqual(model.visible(Filters(),"last",True),[2,100,10])
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            journal=folder/"packets.csv"
            write_packets(journal,rows)
            self.assertEqual(export_csv(journal,len(rows),folder/"full.csv"),len(rows))
            flat=read_rows(folder/"full.csv")
            self.assertEqual(flat[0]["latitude_deg"],"0.0")
            self.assertEqual(flat[3]["latitude_deg"],"")
            self.assertEqual(flat[3]["state_of_charge_percent"],"")
            self.assertEqual(flat[-1]["phy_crc_ok"],"False")
            self.assertEqual(flat[-1]["application_crc_ok"],"True")
            self.assertEqual(export_csv(journal,len(rows),folder/"fleet.csv","fleet"),3)
            self.assertEqual(read_rows(folder/"fleet.csv")[0]["last_available_soc_percent"],"0.0")


class OperatorWorkflow(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder=Path(self.tmp.name)
        self.fixture=ROOT/"examples/epfl_bench.iq"
        self.root=tk.Tk()
        self.root.withdraw()
        self.ui=ReceiverWindow(self.root,"Replay",str(self.fixture))
        self.ui.setup.output.set(str(self.folder/"runs"))
        self.addCleanup(self.dispose)

    def pump(self,condition,timeout=20):
        deadline=time.monotonic()+timeout
        while not condition() and time.monotonic()<deadline:
            self.root.update()
            time.sleep(.015)
        self.assertTrue(condition(),self.ui.detail.cget("text"))

    def dispose(self):
        if self.ui.worker or self.ui.job:
            self.ui.finish()
            self.pump(lambda:not self.ui.worker and not self.ui.job)
        self.ui.close()

    def test_replay_selection_exports_and_new_run(self):
        u=self.ui
        p=u.panels
        u.name.set("Bench A")
        u.start_button.invoke()
        self.pump(lambda:2 in u.model.nodes)
        self.assertIsNotNone(u.worker)
        p.fleet.selection_set("2")
        p.select()
        p.sort_by("last")
        first_count=u.model.total
        p.clear_log()
        self.assertEqual(p.log.get_children(),())
        self.pump(lambda:u.model.total>first_count)
        self.assertEqual((p.selected_id,p.fleet.selection()),(2,("2",)))
        self.assertTrue(p.log.get_children())
        p.id_filter.set("002")
        p.fc_filter.set("914.875")
        p.sf_filter.set("11")
        self.assertEqual(p.fleet.get_children(),("2",))
        self.pump(lambda:u.phase=="saved")
        self.assertEqual(p.selected_id,2)
        self.assertIn("42.",p.lines["coords"].cget("text"))
        self.assertTrue(p.fix.cget("text").startswith("Valid fix"))
        self.assertEqual(len(p.history.get_children()),2)
        self.assertEqual(p.hex_text.get("1.0","end-1c").replace(" ","").lower(),GOLD[1].hex())
        self.assertFalse(u.prr.winfo_manager())
        first=u.path
        journal=read_rows(first/"packets.csv")
        self.assertEqual(len(journal),5)
        for kind,count in (("filtered",2),("all",5),("fleet",3)):
            dest=first/"exports"/f"{kind}.csv"
            with patch("operator_gui.filedialog.asksaveasfilename",return_value=str(dest)):
                u.export_button.invoke()
                dialog=next(w for w in self.root.winfo_children() if isinstance(w,tk.Toplevel) and w.title()=="Export CSV")
                widgets=dialog.winfo_children()[0].winfo_children()
                choices={str(w.cget("value")):w for w in widgets if isinstance(w,ttk.Radiobutton)}
                self.assertTrue(choices["all"].instate(["selected"]))
                choices[kind].invoke()
                next(w for w in widgets if isinstance(w,ttk.Button)).invoke()
            self.pump(lambda:u.job is None)
            exported=read_rows(dest)
            self.assertEqual(len(exported),count)
            self.assertEqual({r["run_id"] for r in exported},{first.name})
            self.assertEqual({r["run_name"] for r in exported},{"Bench A"})
            self.assertTrue(all(r["receive_utc"] and r["payload_hex"] for r in exported))
            if kind=="all":
                self.assertEqual([r["payload_hex"] for r in exported],[r["payload_hex"] for r in journal])
            self.assertEqual(u.open_path,dest.parent)
            self.assertIn(dest.name,u.detail.cget("text"))
            with patch("operator_gui.os.startfile") as open_folder:
                u.folder_button.invoke()
                open_folder.assert_called_once_with(str(dest.parent))
        u.begin_score(self.fixture.with_suffix(".tx.csv"))
        self.pump(lambda:u.job is None)
        self.assertIn("5/5",u.prr.cget("text"))
        self.assertEqual(u.prr.winfo_manager(),"grid")

        old_mail=u.mail
        old_model=weakref.ref(u.model)
        config=u.setup.selected()
        u.new_button.invoke()
        second=u.path
        self.assertTrue(second.is_dir())
        self.assertNotEqual(first,second)
        self.assertEqual(u.setup.selected(),config)
        self.assertEqual(p.filters(),Filters())
        self.assertEqual((u.model.total,len(u.model.nodes)),(0,0))
        self.assertEqual(p.log.get_children(),())
        self.assertFalse(u.prr.winfo_ismapped())
        gc.collect()
        self.assertIsNone(old_model())
        old_mail.put("progress",{"path":str(first),"committed":999})
        short=self.folder/"one.iq"
        with self.fixture.open("rb") as src:
            short.write_bytes(src.read(2*1_150_000))
        Path(str(short)+".json").write_text('{"synthetic":true,"started_utc":"2026-03-01T00:00:00+00:00"}')
        u.setup.capture.set(str(short))
        u.setup.paced.set(False)
        u.start_button.invoke()
        self.pump(lambda:u.phase=="saved")
        self.assertEqual((u.model.total,list(u.model.nodes)),(1,[1]))
        packet=read_rows(second/"packets.csv")[0]
        self.assertEqual(packet["timestamp_source"],"recorded_capture_utc")
        self.assertTrue(packet["receive_utc"].startswith("2026-03-01T00:00:00.183"))
        self.assertEqual(read_rows(first/"packets.csv"),journal)
        self.assertEqual(len(read_rows(first/"exports/all.csv")),5)
        # Reset during active replay, including its stop/drain and old mailbox.
        for _ in range(3):
            u.new_run()
            u.setup.capture.set(str(self.fixture))
            u.setup.paced.set(True)
            u.start()
            retiring=u.path
            u.new_run()
            self.pump(lambda:u.phase=="ready" and not u.pending_new)
            self.assertNotEqual(retiring,u.path)
            self.assertEqual(json.loads((retiring/"summary.json").read_text())["status"],"stopped")
            self.assertEqual((u.model.total,len(u.model.nodes)),(0,0))
        active=[t.name for t in threading.enumerate() if t.name in ("fleet-operator","fleet-export","rtl-reader","iq-recorder")]
        self.assertEqual(active,[])

    def test_gui_one_shot_gain_and_915_pair_setup(self):
        class BenchRadio:
            def __init__(self,profile,gain,ppm,connect,device):
                self.p,self.gain,self.ppm=profile,gain,ppm
                self.header=b"RTL0"+struct.pack("!II",5,29)
                self.sock=self
                self.db=gain
            def open(self,folder,stop):
                pass
            def close(self):
                pass
            def command(self,key,value):
                if key==4:
                    self.db=value/10
            def recv(self,n):
                pattern=bytes((0,0,255,255) if self.db>1 else (123,123,131,131))
                return (pattern*(n//4+1))[:n]
        u=self.ui
        u.mode.set("Live RTL-SDR")
        u.setup.carriers[914875000].set(False)
        u.setup.carriers[915125000].set(False)
        u.setup.sfs[10].set(False)
        u.setup.refresh()
        self.assertTrue(u.setup.auto.get())
        self.assertIn("disabled",u.setup.gain_input.state())
        def finite(*args,**kwargs):
            return live(*args,seconds=.1,evidence="synthetic gain-dependent IQ",**kwargs)
        with patch("run.Radio",BenchRadio),patch("operator_gui.live",finite):
            u.start()
            self.pump(lambda:u.phase in ("saved","error"))
        self.assertEqual(u.phase,"saved",u.detail.cget("text"))
        summary=json.loads((u.path/"summary.json").read_text())
        self.assertEqual(set(summary["branches"]),{"915000000/SF11","915000000/SF12"})
        self.assertLessEqual(summary["gain_db"],1.)
        self.assertGreater(len(summary["gain_setup"]["observations"]),1)
        self.assertEqual((u.path/"capture.iq").stat().st_size,200000)
        self.assertEqual(summary["recording"]["queued_bytes"],0)
        self.assertEqual(u.recording.cget("text"),"IQ saved")

    def test_detail_retains_a_valid_position_and_log_clear_is_local(self):
        u=self.ui
        u.model.ingest(row(GOLD[0],2.5))
        no_fix=bytearray(payload(1,1)[:-2])
        no_fix[:8]=bytes(8)
        no_fix[52]&=~0x10
        no_fix.extend(binascii.crc_hqx(no_fix,0xffff).to_bytes(2,"little"))
        u.model.ingest(row(no_fix,9))
        u.panels.render(u.model,12)
        self.assertEqual(u.panels.fix.cget("text"),"No fix   ·   last valid position 00:09.5 ago")
        self.assertIn("42.4351455",u.panels.lines["coords"].cget("text"))
        self.assertEqual(u.panels.lines["soc"].cget("text"),"99")
        u.panels.pause.set(True)
        u.panels.log.selection_set("2")
        with patch.object(u.panels.log,"clipboard_clear"),patch.object(u.panels.log,"clipboard_append") as copied:
            u.panels.copy_row()
        self.assertIn(no_fix.hex(),copied.call_args.args[0])
        u.panels.clear_log()
        self.assertEqual(u.model.total,2)
        self.assertEqual(len(u.panels.history.get_children()),2)
        self.assertEqual(u.panels.log.get_children(),())

    def test_new_run_waits_for_a_pending_export(self):
        u=self.ui
        u.setup.paced.set(False)
        u.start()
        self.pump(lambda:u.phase=="saved")
        previous=u.path
        saving,release=threading.Event(),threading.Event()
        def held(*args):
            saving.set()
            if not release.wait(5):
                raise RuntimeError("Export test timed out")
            return export_csv(*args)
        with patch("operator_gui.export_csv",held):
            u.begin_export("all",previous/"exports"/"all.csv")
            self.pump(saving.is_set)
            u.new_run()
            self.assertEqual(u.path,previous)
            release.set()
            self.pump(lambda:not u.pending_new)
        self.assertNotEqual(u.path,previous)
        self.assertEqual(len(read_rows(previous/"exports"/"all.csv")),5)
        self.assertEqual(u.model.total,0)

    def test_sic_switches_the_view_to_the_merged_journal(self):
        u=self.ui
        u.setup.sic.set(True)
        u.setup.paced.set(False)
        u.start()
        self.pump(lambda:u.phase in ("saved","error"))
        self.assertEqual(u.phase,"saved",u.detail.cget("text"))
        primary=read_rows(u.path/"packets_primary.csv")
        merged=read_rows(u.path/"packets.csv")
        self.assertEqual(len(primary),5)
        self.assertEqual((u.model.total,u.cursor.count),(len(merged),len(merged)))
        self.assertEqual(u.journal.name,"packets.csv")
        self.assertEqual([p.row["payload_hex"] for _,p in u.model.log],[r["payload_hex"] for r in merged])

