import binascii
import csv
from contextlib import redirect_stdout
import gc
import io
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
from fixtures import NINE_BRANCH_PROFILE, Event, GOLD, generate, payload
from fleet_view import Fleet, Filters, HISTORY_LIMIT, LOG_LIMIT, export_csv
from front_end import FRONT_END_LABELS, FrontEndPreferences, preset
from fleet import main as fleet_cli
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
        self.preferences = FrontEndPreferences(self.folder/"settings.sqlite3")
        self.ui=ReceiverWindow(self.root,"Replay",str(self.fixture),front_preferences=self.preferences)
        self.ui.setup.path.set(str(NINE_BRANCH_PROFILE))
        self.ui.setup.output.set(str(self.folder/"runs"))
        self.addCleanup(self.dispose)

    def pump(self,condition,timeout=20):
        deadline=time.monotonic()+timeout
        while not condition() and time.monotonic()<deadline:
            self.root.update()
            time.sleep(.015)
        self.assertTrue(condition(),self.ui.detail.cget("text"))

    def test_rf_panel_shows_per_branch_suspect_packets(self):
        panel = self.ui.rf_panel
        # The operator window stays withdrawn throughout this test.
        with patch.object(panel, "winfo_ismapped", return_value=True):
            panel.update_data({"branches": {"915000000/SF10": {"suspect_packets": 2},
                                             "915000000/SF12": {"suspect_packets": 0}}})
            self.assertEqual(panel.tree.set("915000000/SF10","suspect_packets"),"2")
            self.assertEqual(panel.tree.set("915000000/SF12","suspect_packets"),"0")
            panel.update_data({"branches": {"915000000/SF10": {}}})
            self.assertEqual(panel.tree.set("915000000/SF10","suspect_packets"),"—")

    def dispose(self):
        if self.ui.worker or self.ui.job:
            self.ui.finish()
            self.pump(lambda:not self.ui.worker and not self.ui.job)
        self.ui.close()

    def select_profile(self, index):
        box = self.ui.monitor_box
        box.current(index)
        box.event_generate("<<ComboboxSelected>>")

    def test_front_end_selection_persistence_cli_and_gain_reminder(self):
        u,setup = self.ui,self.ui.setup
        u.mode.set("Live RTL-SDR")
        self.preferences.save("lana",preset("lana"),received=True)
        u.auto_check.invoke()
        expected = (("lana","Nooelec LaNA",20.,"USB"),("lana-wb","Nooelec LaNA WB",19.,"USB"),
                    ("none","None",0.,"None"))
        for key,model,gain,power in expected:
            setup.front_picker.set(FRONT_END_LABELS[key])
            setup.front_picker.event_generate("<<ComboboxSelected>>")
            self.assertTrue(all(e.instate(["readonly"]) for e in setup.front_entries.values()))
            meta = setup.front_end()
            self.assertEqual(meta,dict(lna_model=model,nominal_gain_db=gain,power_source=power,filter="None",
                                       usb_supply=None if key=="none" else "Unspecified"))
            self.assertEqual(u.health["Front end"].cget("text"),"Antenna direct" if key=="none" else model)
            self.assertEqual(bool(u.gain_notice.winfo_manager()),key!="lana")
            setup.done()
            self.assertEqual(FrontEndPreferences(self.preferences.path).selected(),(key,meta))
            with patch("run.live") as call, patch("fleet.FrontEndPreferences",return_value=self.preferences), \
                    patch("fleet.signal.signal"),redirect_stdout(io.StringIO()):
                fleet_cli(["live","--front-end",key])
                self.assertEqual(call.call_args.kwargs["front_end"],meta)
        self.assertEqual(self.preferences.read("last_run"),preset("lana"))
        u.auto_check.invoke()
        self.assertFalse(u.gain_notice.winfo_manager())
        setup.front_picker.set("Custom")
        setup.front_picker.event_generate("<<ComboboxSelected>>")
        self.assertTrue(all(e.instate(["!readonly","!disabled"]) for e in setup.front_entries.values()))
        for k,v in dict(lna_model="Bench LNA",nominal_gain_db="14.5",power_source="USB",
                        filter="915 MHz",usb_supply="battery bank").items():
            setup.front_vars[k].set(v)
        setup.done()
        self.assertEqual(FrontEndPreferences(self.preferences.path).selected(),("custom",setup.front_end()))
        with patch("run.live") as call, patch("fleet.FrontEndPreferences",return_value=self.preferences), \
                patch("fleet.signal.signal"),redirect_stdout(io.StringIO()):
            fleet_cli(["live","--front-end","lana-wb","--lna-gain-db","18.5","--rf-filter","915 MHz"])
            self.assertEqual(call.call_args.kwargs["front_end"],
                             dict(preset("lana-wb"), nominal_gain_db=18.5, filter="915 MHz"))
        u.mode.set("IQ replay")
        self.assertEqual(u.health["Front end"].cget("text"),"Front end not recorded")

    def test_replay_selection_exports_and_new_run(self):
        u=self.ui
        p=u.panels
        u.name.set("Bench A")
        u.start_button.invoke()
        self.pump(lambda:2 in u.model.nodes)
        self.assertIsNotNone(u.worker)
        p.fleet.selection_set("2")
        p.select()
        p.sort_by("age")
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
                # A transmitter starts during the two-second check, after a quiet scan.
                burst=n==4_000_000 and self.db>=7.7
                pattern=bytes((0,0,255,255) if burst else (123,123,131,131))
                return (pattern*(n//4+1))[:n]
        u=self.ui
        u.mode.set("Live RTL-SDR")
        self.select_profile(0)
        u.setup.sfs[10].set(False)
        u.setup.refresh()
        for control in (u.record_check, u.auto_check, u.gain_input, u.monitor_box):
            self.assertEqual(control.winfo_toplevel(), self.root)
        self.assertTrue(u.record_check.instate(["selected", "!disabled"]))
        self.assertTrue(u.auto_check.instate(["selected", "!disabled"]))
        self.assertTrue(u.gain_input.instate(["disabled"]))
        def finite(*args,**kwargs):
            return live(*args,seconds=.1,evidence="synthetic gain-dependent IQ",**kwargs)
        with patch("run.Radio",BenchRadio),patch("operator_gui.live",finite):
            u.start_button.invoke()
            for control in (u.record_check, u.auto_check, u.gain_input, u.monitor_box):
                self.assertTrue(control.instate(["disabled"]))
            self.pump(lambda:u.phase in ("saved","error"))
        self.assertEqual(u.phase,"saved",u.detail.cget("text"))
        summary=json.loads((u.path/"summary.json").read_text())
        self.assertEqual(set(summary["branches"]),{"915000000/SF11","915000000/SF12"})
        scan=summary["gain_setup"]
        checks=scan["headroom_checks"]
        self.assertEqual(len(checks),2)
        self.assertGreater(checks[0]["clip_fraction"],0)
        self.assertEqual(checks[-1]["clip_fraction"],0)
        self.assertLess(checks[-1]["gain_db"],checks[0]["gain_db"])
        self.assertEqual(checks[-1]["gain_db"],0)
        self.assertIn("minimum tuner gain",scan["warning"])
        self.assertEqual(summary["gain_db"],checks[-1]["gain_db"])
        sidecar=json.loads((u.path/"capture.iq.json").read_text())
        self.assertEqual(sidecar["gain_db"],summary["gain_db"])
        self.assertEqual(sidecar["front_end"],summary["front_end"])
        self.assertEqual(sidecar["front_end"]["power_source"],"USB")
        self.assertEqual((u.path/"capture.iq").stat().st_size,200000)
        self.assertEqual(summary["recording"]["queued_bytes"],0)
        self.assertEqual(u.recording.cget("text"),"IQ saved")

        u.new_button.invoke()
        u.record_check.invoke()
        self.assertEqual(u.recording.cget("text"), "Off")
        u.auto_check.invoke()
        self.assertTrue(u.gain_input.instate(["!disabled"]))
        u.gain_input.delete(0, "end")
        u.gain_input.insert(0, "nan")
        with patch("operator_gui.messagebox.showerror") as error:
            u.start_button.invoke()
            error.assert_called_once()
        self.assertEqual(u.phase, "ready")
        u.gain_input.delete(0, "end")
        u.gain_input.insert(0, "28.0")
        u.auto_check.invoke()
        u.auto_check.invoke()
        self.assertEqual(u.gain_input.get(), "28.0")
        with patch("run.Radio", BenchRadio), patch("operator_gui.live", finite):
            u.start_button.invoke()
            self.pump(lambda: u.phase in ("saved", "error"))
        self.assertEqual(u.phase, "saved", u.detail.cget("text"))
        manual = json.loads((u.path/"summary.json").read_text())
        self.assertFalse(manual["auto_gain_requested"])
        self.assertEqual(manual["gain_db"], 28)
        self.assertIsNone(manual["recording"])
        self.assertTrue((u.path/"packets.csv").is_file())
        self.assertFalse((u.path/"capture.iq").exists())
        u.new_button.invoke()
        self.assertTrue(u.gain_input.instate(["!disabled"]))
        self.assertTrue(u.record_check.instate(["!selected"]))

    def test_monitor_presets_replay_all_twelve_branches(self):
        u = self.ui
        self.select_profile(1)
        names = u.monitor_box.cget("values")
        plans = ((915_000_000,), (914_875_000, 915_125_000),
                 (914_700_000, 914_900_000, 915_100_000, 915_300_000))
        sfs = (10, 11, 12)
        self.assertEqual(tuple(names),("915 MHz only","Fleet A/B","Four-carrier","Custom"))
        self.assertEqual(tuple(u.setup.carrier_picker.cget("values")),tuple(names))
        self.assertEqual(u.monitor_box.current(), 1)
        self.assertEqual(u.setup.selected().max_cfo_hz,18000)
        for text in ("9999","30001"):
            u.setup.max_cfo.set(text)
            with self.assertRaises(ValueError):
                u.setup.selected()
        u.setup.max_cfo.set("28000")
        self.assertEqual(u.setup.selected().max_cfo_hz,28000)
        u.setup.max_cfo.set("18000")
        self.assertFalse(u.live_controls.winfo_manager())
        for i, carriers in enumerate(plans):
            # Any per-run overrides must be cleared by the next preset selection.
            u.setup.sfs[7].set(True)
            u.setup.sfs[12].set(False)
            u.setup.extra.set("914.95")
            self.assertEqual(u.monitor_box.get(), "Custom")
            self.select_profile(i)
            p = u.setup.selected()
            self.assertEqual((p.center_hz, p.carriers_hz, p.spreading_factors), (915_000_000, carriers, sfs))
            self.assertEqual(set(u.panels.cells), {(fc, sf) for fc in carriers for sf in sfs})
            self.assertEqual(u.monitor_box.current(), i)
            u.setup.save_carriers()
            self.assertEqual(self.preferences.carrier_profile(p).carriers_hz,carriers)

        # All twelve DeSCENT packets start together in a single 1 MS/s CU8 capture.
        events = [Event(.124723, fc, sf, payload(1+3*i+j, 17), .10, (-2345, 1713, 423)[j],
                        (32, -45, 38)[j], j+.3*i)
                  for i, fc in enumerate(plans[2]) for j, sf in enumerate(sfs)]
        iq = self.folder / "four_carrier.iq"
        generate(iq, p, events, 3.51)
        u.setup.capture.set(str(iq))
        u.setup.paced.set(False)
        u.start_button.invoke()
        self.assertTrue(u.monitor_box.instate(["disabled"]))
        self.pump(lambda: u.phase in ("saved", "error"))
        self.assertEqual(u.phase, "saved", u.detail.cget("text"))
        rows = read_rows(u.path / "packets.csv")
        self.assertEqual(len(rows), 12)
        self.assertEqual(len(u.model.nodes), 12)
        self.assertEqual({(int(r["carrier_hz"]), int(r["sf"]), r["payload_hex"]) for r in rows},
                         {(e.carrier, e.sf, e.data.hex()) for e in events})
        self.assertTrue(all(r["phy_crc_ok"] == r["application_crc_ok"] == "True" for r in rows))

        saved = u.path
        u.new_button.invoke()
        self.assertEqual(u.monitor_box.current(), 2)
        self.select_profile(0)
        u.field.open_saved(saved)
        self.pump(lambda: u.phase == "saved")
        self.assertEqual(u.monitor_box.current(), 2)
        self.assertTrue(u.monitor_box.instate(["disabled"]))
        u.new_button.invoke()
        self.assertEqual(u.monitor_box.current(), 0)
        self.assertTrue(u.monitor_box.instate(["readonly", "!disabled"]))

    def test_monitor_presets_respect_recorded_tuning(self):
        u = self.ui
        iq = self.folder / "retuned.iq"
        iq.write_bytes(bytes([127, 128]))
        Path(str(iq)+".json").write_text('{"center_hz":915200000}')
        u.setup.capture.set(str(iq))
        self.select_profile(2)
        self.assertEqual(u.setup.tuner.get(), "915.200")
        with self.assertRaisesRegex(ValueError, "exceeds the capture"):
            u.setup.selected()
        with patch("operator_gui.messagebox.showerror") as error:
            u.start_button.invoke()
            error.assert_called_once()
        self.assertIsNone(u.worker)
        self.select_profile(0)
        self.assertEqual(u.setup.selected().center_hz, 915_200_000)
        u.mode.set("Live RTL-SDR")
        self.assertEqual(u.setup.selected().center_hz, 915_000_000)
        self.assertEqual(u.live_controls.winfo_manager(), "pack")

    def test_detail_retains_a_valid_position_and_log_clear_is_local(self):
        u=self.ui
        u.model.ingest(row(GOLD[0],2.5))
        no_fix=bytearray(payload(1,1)[:-2])
        no_fix[:8]=bytes(8)
        no_fix[52]&=~0x10
        no_fix.extend(binascii.crc_hqx(no_fix,0xffff).to_bytes(2,"little"))
        u.model.ingest(row(no_fix,9))
        u.panels.render(u.model,12)
        self.assertEqual(u.panels.fix.cget("text"),"No fix   ·   last valid report 00:09.5 ago")
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

