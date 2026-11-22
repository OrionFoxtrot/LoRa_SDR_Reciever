import binascii
import csv
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from config import ROOT,read_profile
from field_acceptance import science
from fixtures import Event,generate
from fleet_view import Fleet,Packet
from generic_view import escaped
from mission import Mission,PeriodBook,fix_time
from sic import merge_passes
from observations import east_north
from operator_gui import ReceiverWindow
from front_end import FrontEndPreferences
from operator_panels import validity_bar
from receiver import Receiver
from run import replay
from reports import telemetry,write_json,write_packets
from sources import Layout,complex_iq


def frame(data,t,fc=915000000,sf=11,phy=True,**more):
    return telemetry(dict(start_sample=round(t*1e6),capture_s=t,carrier_hz=fc,sf=sf,
             phy_crc_ok=phy,payload_hex=data.hex(),preamble_power_dbfs=-20-t/2,cfo_hz=30+t,**more),"primary")


def payload_with_flags(node,mask,damage=False,*,seq=0):
    data=bytearray(53)
    data[48:50]=seq.to_bytes(2,"little")
    data[50]=node
    data[52]=mask
    checksum=binascii.crc_hqx(data,0xffff) ^ int(damage)
    return bytes(data)+checksum.to_bytes(2,"little")


class CoverageTests(unittest.TestCase):
    def test_latest_validity_byte_per_heard_id(self):
        model=Fleet()
        self.addCleanup(model.close)

        def report(node,t,mask,phy=True,damage=False):
            model.ingest(frame(payload_with_flags(node,mask,damage),t,phy=phy))

        report(1,1,0)
        report(2,2,0x80)
        self.assertEqual((len(model.nodes),model.reporting_valid_data),(2,0))
        for bit in range(7):
            with self.subTest(sensor_bit=bit):
                report(1,3+bit,1<<bit)
                self.assertEqual(model.reporting_valid_data,1)  # All measurements are zero.
                self.assertEqual(model.nodes[1].latest.sensor_mask, 1 << bit)
        report(2,35,0xA0)
        self.assertEqual(model.reporting_valid_data,2)
        report(1,40,0)
        report(1,30,0x7F)  # This packet finished decoding late.
        report(2,41,0,phy=False)
        report(2,42,0,damage=True)
        report(3,43,0x7F,phy=None)
        model.ingest(frame(b"hello",44))
        self.assertEqual((len(model.nodes),model.reporting_valid_data),(2,1))
        self.assertEqual((model.total, model.failed), (16, 2))
        report(4,45,0x7F,phy=False,damage=True)
        self.assertEqual((model.total, model.failed), (17, 3))  # Both CRCs fail in one frame.

    def test_paced_gap_clock_and_stop_use_capture_time(self):
        p=replace(read_profile(),carriers_hz=(915000000,),spreading_factors=(10,))
        with tempfile.TemporaryDirectory() as tmp:
            iq=Path(tmp)/"gap.iq"
            iq.write_bytes(bytes((127,128))*200_000)
            write_json(str(iq)+".json",dict(synthetic=True,bytes_received=4_400_000,bytes_dropped=4_000_000,
                                           gaps=[dict(file_offset=200_000,dropped_bytes=4_000_000)]))
            stopped=threading.Event()
            snapshots=[]
            def notify(d):
                snapshots.append(d)
                if .2<d["capture_s"]<2.1:
                    stopped.set()
            folder=replay(p,iq,Path(tmp)/"runs",stop=stopped,paced=True,notify=notify)
            s=json.loads((folder/"summary.json").read_text())
            self.assertEqual(s["status"],"stopped")
            self.assertGreater(s["capture_seconds"],.2)
            self.assertLess(s["capture_seconds"],2.1)
            self.assertEqual(s["processed_samples"],100_000)
            self.assertEqual(s["gap_ranges_s"],[[.1,s["capture_seconds"]]])
            self.assertTrue(all(v["capture_s"]<=s["capture_seconds"]+.000001 for v in snapshots))

    def test_sic_replacement_across_adjacent_sample_buckets(self):
        def picked(sample,payload,passed):
            return {"start_sample":sample,"carrier_hz":915000000,"sf":11,"payload_hex":payload,"phy_crc_ok":passed}
        base=[picked(64,"bad",False),picked(350,"ac",True)]
        repair=picked(126,"ab",True)
        merged,repeats=merge_passes(base,[repair,picked(181,"ab",True),picked(350,"ac",True)])
        self.assertEqual([r["payload_hex"] for r in merged],["ab","ac"])
        self.assertEqual(repeats,2)
        with self.assertRaises(InterruptedError):
            merge_passes(base,[repair],cancelled=lambda:True)
        donor=dict(picked(50000,"abcd",True),sf=10,bandwidth_hz=125000)
        shifted=dict(donor,start_sample=48821)
        repeat=dict(donor,start_sample=1050000)
        other_payload=dict(shifted,payload_hex="0000")
        result,n=merge_passes([donor],[shifted,other_payload,repeat])
        self.assertEqual((len(result),n),(3,1))

    def test_pending_boundary_watermark_and_capture_gaps(self):
        profile=replace(read_profile(),carriers_hz=(915000000,),spreading_factors=(11,))
        m=Mission(2,1,2,0.1,"mission-aligned",roster={1:{"sf":"11"},2:{"sf":"11"}})
        book=PeriodBook(m)
        def receive(r):
            book.hear(Packet.read(telemetry(r,"primary")))
        rx=Receiver(profile,receive)
        with tempfile.TemporaryDirectory() as tmp:
            iq=Path(tmp)/"edge.iq"
            generate(iq,profile,[Event(.91,915000000,11,science(1,0))],3.5)
            crossed=False
            arrived=False
            for start,raw in Layout.read(iq).blocks(iq,50000):
                rx.feed(start,complex_iq(raw))
                now=(start+len(raw)//2)/1e6
                info=book.info(0,now,rx.settled_before()/1e6,False)
                if 1.1<now<1.5:
                    crossed=True
                    self.assertEqual((info["heard"],info["state"],info["final"]),(0,"Resolving packets",False))
                if info["heard"]:
                    arrived=True
                    self.assertGreater(now,1.1)
            self.assertTrue(crossed and arrived)
            rx.close_segment()
        self.assertEqual(book.report(3.5)[1]["heard"],1)
        self.assertEqual([x["partial"] for x in book.report(3.5)], [True,False,False,False,True])
        self.assertTrue(book.report(3.5,[(.9,1.01)])[1]["gap"])
        self.assertEqual(book.roster_rows(0),[(2,None,{"sf":"11"})])
        self.assertEqual(Mission().aligned({"started_utc":"2026-01-01T00:00:00+00:00",
            "mission_epoch_utc":"2026-01-01T00:00:02+00:00"}).epoch,2.)

    def test_timestamped_fix_loss_return_and_local_axes(self):
        model=Fleet(Mission(2,5,2))
        self.addCleanup(model.close)
        for n,t,valid,stamp in ((1,1,True,1),(2,2,True,2),(1,7,True,1),(1,8,False,None),(1,12,True,12)):
            r=frame(science(n,int(t),moving=False,fix=valid),t)
            d=json.loads(r["telemetry_json"])
            if stamp is not None:
                d["gps_fix_capture_s"]=stamp
            r["telemetry_json"]=json.dumps(d)
            model.ingest(r)
            if t==8:
                self.assertEqual(fix_time(model.nodes[1].position),(1,True))
        self.assertEqual([r["gps"] for r in model.book.report(15)],[2,0,1])
        self.assertEqual(model.first_fix.node,1)
        origin=(42.,-76.)
        self.assertEqual(east_north(origin,*origin),(0.,0.))
        east,north=east_north(origin,42.,-75.999)
        self.assertAlmostEqual(east,82.85,delta=.1)
        self.assertAlmostEqual(north,0,delta=.1)
        self.assertAlmostEqual(east_north(origin,42.001,-76.)[1],111.07,delta=.1)


class FieldWorkflow(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)
        self.root=tk.Tk()
        self.root.withdraw()
        self.ui=ReceiverWindow(self.root,"Replay",str(ROOT/"examples/epfl_bench.iq"),
                               front_preferences=FrontEndPreferences(self.path/"settings.sqlite3"))
        self.ui.setup.path.set(str(ROOT/"tests/nine_branch_profile.json"))
        self.ui.setup.output.set(str(self.path/"runs"))
        self.addCleanup(self.clean)

    def pump(self,test,limit=20):
        end=time.monotonic()+limit
        while not test() and time.monotonic()<end:
            self.root.update()
            time.sleep(.015)
        self.assertTrue(test(),self.ui.detail.cget("text"))

    def clean(self):
        u=self.ui
        if u.worker:
            u.finish()
        if u.job_kind=="sic":
            u.job_stop.set()
        self.pump(lambda:not u.worker and not u.job)
        u.close()

    def saved(self):
        archive=self.path/"archived"
        archive.mkdir()
        records=[frame(b"hello",.4),frame(b"\x00\xff\0A",.8),
                 frame(science(1,0),1),frame(bytes(range(55)),1.2),frame(b"bad",1.4,phy=False),
                 frame(science(197,1),1.6,phy=None)]
        for i,t in enumerate((2,5,22,24)):
            records.append(frame(science(2,i,moving=False),t,915125000,10))
            records.append(frame(science(1,i+1,fix=i!=2),t+.2))
        records.append(frame(payload_with_flags(2,0x80),28,915125000,10))
        write_packets(archive/"packets.csv",records)
        m=Mission(2,10,2,station=(42.451,-76.478))
        write_json(archive/"summary.json",dict(status="complete",mode="replay",run_name="Synthetic field bench",
            evidence="synthetic IQ replay",profile=read_profile(ROOT/"tests/nine_branch_profile.json").report(),mission=m.dump(),
            capture_seconds=30,packets=len(records),gap_ranges_s=[[9,11]]))
        return archive,records

    def test_fleet_row_sensor_validity(self):
        u=self.ui
        panel=u.panels
        tree=panel.fleet
        self.assertEqual(tuple(tree.cget("columns")),("id","age","carrier","sf","counter","rssi","snr","soc","validity"))
        self.assertEqual(tree.heading("age", "text"), "Last Heard")
        u.model.ingest(frame(payload_with_flags(1,0xB1),4,rssi_dbfs=-31.26,snr_db=0))  # Measured zero SoC and SNR.
        u.model.ingest(frame(payload_with_flags(2,0x80),5,rssi_dbfs=-9.1,snr_db=-12.6))
        u.model.ingest(frame(payload_with_flags(3,0x7F),3))
        older=frame(payload_with_flags(4,0x40),2)
        metadata=json.loads(older["telemetry_json"])
        del metadata["validity_raw"]
        older["telemetry_json"]=json.dumps(metadata)
        u.model.ingest(older)
        missing=frame(payload_with_flags(5,0),1)
        missing["telemetry_json"]=missing["payload_hex"]=""
        missing["packet_counter"]=""
        u.model.ingest(missing)
        panel.render(u.model,7)
        panel.sort_by("age")
        self.assertEqual(tree.get_children(), ("2","1","3","4","5"))
        self.assertEqual(tree.set("2","age"), "2.0 s ago")
        def bar(node):
            return tree.set(node,"validity")
        self.assertEqual(bar("1"),validity_bar(0x31))
        self.assertEqual(tree.set("1","soc"),"0")
        self.assertEqual((tree.set("1","counter"),tree.set("1","rssi"),tree.set("1","snr")),("0","-31.3","0.0"))
        self.assertEqual(bar("2"),validity_bar(0))
        self.assertEqual(tree.set("2","soc"),"—")
        self.assertEqual(bar("3"),validity_bar(0x7F))
        self.assertEqual(bar("4"),validity_bar(0x40))
        self.assertEqual(bar("5"),"—")
        self.assertEqual((tree.set("5","counter"),tree.set("5","rssi"),tree.set("5","snr")),("—","—","—"))
        panel.sort_by("rssi")
        self.assertEqual(tree.get_children()[:2],("2","1"))
        panel.sort_by("snr")
        self.assertEqual(tree.get_children()[:2],("1","2"))
        tree.selection_set("5")
        panel.select()
        self.assertEqual(panel.hex_text.get("1.0","end-1c"),"—")
        tree.selection_set("1")
        panel.select()
        panel.sort_by("validity")
        self.assertEqual(tree.get_children(),("3","1","4","2","5"))
        panel.sort_by("validity")
        self.assertEqual(tree.get_children(),("2","4","1","3","5"))
        u.model.ingest(frame(payload_with_flags(1,0x80,seq=42),8,rssi_dbfs=-24.1,snr_db=-3.2))
        u.model.ingest(frame(payload_with_flags(1,0x7F),7,rssi_dbfs=-1,snr_db=16))  # Older RF time.
        u.model.ingest(frame(payload_with_flags(1,0x7F),9,phy=False,rssi_dbfs=-6,snr_db=6))
        panel.render(u.model,10)
        self.assertEqual(tree.selection(),("1",))
        self.assertEqual(bar("1"),validity_bar(0))
        self.assertEqual((tree.set("1","counter"),tree.set("1","rssi"),tree.set("1","snr")),("42","-24.1","-3.2"))
        self.assertIn("Counter 42",panel.meta.cget("text"))
        self.assertIn("RSSI -24.1 dBFS",panel.meta.cget("text"))
        self.assertEqual(tree.set("1","soc"),"0")
        self.assertEqual(tree.set("1","age"),"2.0 s ago")

    def test_lettered_tiles_follow_rows_clicks_and_scroll(self):
        u=self.ui
        panel=u.panels
        tree,canvas=panel.fleet,panel.validity_tiles
        masks=[1<<i for i in range(7)]
        for node in range(1,55):
            u.model.ingest(frame(payload_with_flags(node,masks[(node-1)%7]),node))
        self.root.overrideredirect(True)
        self.root.geometry("1366x768+2200+40")
        self.root.deiconify()
        panel.render(u.model,55)
        self.root.update()
        def check_letters(node):
            x,y,w,h=tree.bbox(str(node),"validity")
            mid=y+h/2-canvas.winfo_y()
            letters=[i for i in canvas.find_withtag("letter") if canvas.coords(i)[1]==mid]
            self.assertEqual("".join(canvas.itemcget(i,"text") for i in letters),"AGMQPCE")
            return [canvas.itemcget(i,"fill")=="#ffffff" for i in letters]
        for node in range(1,8):
            self.assertEqual(check_letters(node),[i==node-1 for i in range(7)])
        tree.see("54")
        self.root.update()
        self.assertEqual(check_letters(54),[i==(54-1)%7 for i in range(7)])
        x,y,w,h=tree.bbox("54","validity")
        canvas.event_generate("<Button-1>",x=w//2,y=y+h//2-canvas.winfo_y())
        self.root.update()
        self.assertEqual((panel.selected_id,tree.selection()),(54,("54",)))
        panel.clear_filters()
        panel.id_filter.set("54")
        self.root.update()
        self.assertEqual(check_letters(54),[i==(54-1)%7 for i in range(7)])
        panel.id_filter.set("100")
        self.root.update()
        self.assertFalse(canvas.winfo_ismapped())

    def test_archive_generic_plot_filter_and_export(self):
        u=self.ui
        archive,records=self.saved()
        with patch("operator_gui.replay",side_effect=AssertionError("IQ access during archive inspection")):
            u.field.open_saved(archive)
            self.pump(lambda:u.phase=="saved")
        self.assertEqual(u.listening_light.cget("text"), "●  Not listening")
        self.assertEqual(u.model.total,len(records))
        self.assertEqual(set(u.model.nodes),{1,2})
        def counts():
            return tuple(w.cget("text") for w in (u.heard_count,u.valid_count,u.packet_count,u.failed_count))
        self.assertEqual(counts(),("2","1",str(len(records)),"2"))
        u.panels.clear_log()
        self.assertEqual(counts(),("2","1",str(len(records)),"2"))
        self.assertTrue(u.model.book.report(30,u.snapshot["gaps"])[1]["gap"])
        u.views.select(1)
        u.generic.render(u.model)
        self.assertEqual([p.row["payload_hex"] for _,p in u.generic.rows],
                         [r["payload_hex"] for r in records if r["phy_crc_ok"] is True])
        self.assertIn("00 FF 00 41 | "+escaped(b"\x00\xff\0A"),u.generic.list.get(1))
        u.generic.clear()
        self.assertFalse(u.generic.rows)
        dest=self.path/"generic.csv"
        u.begin_export("generic",dest)
        self.pump(lambda:u.job is None)
        with dest.open(encoding="utf-8-sig",newline="") as file:
            exported=list(csv.DictReader(file))
        self.assertEqual(len(exported),len(records)-2)
        self.assertTrue(all(r["run_id"]=="archived" for r in exported))
        self.assertEqual(exported[3]["application_crc_ok"],"False")
        u.views.select(0)
        self.root.overrideredirect(True)
        self.root.geometry("1366x768+2200+40")
        self.root.deiconify()
        self.root.update()
        u.panels.cells[(915000000,11)].event_generate("<Button-1>")
        self.root.update()
        self.assertEqual(u.panels.fleet.get_children(),("1",))
        self.assertEqual(counts(),("2","1",str(len(records)),"2"))
        self.assertIn("1 row shown",u.panels.totals.cget("text"))
        u.panels.clear_filters()
        plot=u.panels.plots
        u.panels.detail_book.select(plot.trend)
        self.root.update()
        plot.refresh(u.model,1,30,force=True)
        self.assertEqual(len(plot.picks["trend"]),5)
        first=plot.picks["trend"][0]
        plot.click("trend",first[0],first[1])
        self.assertFalse(plot.follow.get())
        self.assertIn("primary",plot.notes["trend"].cget("text"))
        bounds=plot.bounds
        plot.refresh(u.model,1,45,force=True)
        self.assertEqual(bounds,plot.domain)
        plot.resume()
        plot.metric.set("Both altitudes")
        plot.refresh(u.model,1,30,force=True)
        self.assertEqual(len(plot.picks["trend"]),9)
        u.panels.detail_book.select(plot.track)
        self.root.update()
        plot.refresh(u.model,2,30,force=True)
        coords={(round(x,4),round(y,4)) for x,y,_,_ in plot.picks["track"]}
        self.assertEqual(len(coords),1)
        origin=plot.captions["track"].cget("text").splitlines()[0]
        plot.refresh(u.model,1,30,force=True)
        self.assertGreater(len({(x,y) for x,y,_,_ in plot.picks["track"]}),1)
        self.assertEqual(origin,plot.captions["track"].cget("text").splitlines()[0])
        vals=u.model.observations.series(1,0,30)[0]
        self.assertEqual(sum(p["gps"] is None for p in vals),1)
        u.panels.fleet.selection_set("1")
        u.begin_export("selected",self.path/"history.csv")
        self.pump(lambda:u.job is None)
        with (self.path/"history.csv").open(encoding="utf-8-sig") as f:
            self.assertEqual(len(list(csv.DictReader(f))),5)
        u.new_run()
        self.assertEqual(counts(),("0","0","0","0"))
        self.assertFalse(u.model.nodes)
        self.assertFalse(u.panels.plots.picks["track"])
        self.assertEqual(u.views.index("current"),0)

    def test_replay_pause_then_cancel_sic_and_reopen_merged(self):
        u=self.ui
        u.start()
        self.pump(lambda:u.snapshot.get("capture_s",0)>.1)
        u.toggle_pause()
        end=time.monotonic()+.65
        self.pump(lambda:time.monotonic()>end)
        a=u.snapshot["capture_s"]
        end=time.monotonic()+.55
        self.pump(lambda:time.monotonic()>end)
        self.assertEqual(a,u.snapshot["capture_s"])
        u.toggle_pause()
        self.pump(lambda:u.model.total>=1)
        self.assertEqual(u.listening_light.cget("text"), "●  Not listening")
        u.finish()
        self.pump(lambda:u.phase=="saved")
        base=u.path
        primary=(base/"packets.csv").read_bytes()
        u.field.start_sic()
        u.field.start_sic()
        self.pump(lambda:u.job is None)
        self.assertEqual(u.path,base)
        self.assertEqual(primary,(base/"packets.csv").read_bytes())
        u.field.start_sic()
        self.pump(lambda:u.job is None and u.field.comparison and u.phase=="saved")
        self.assertEqual(primary,(base/"packets.csv").read_bytes())
        self.assertEqual(u.field.label,"Replay + SIC")
        self.assertEqual(u.field.comparison["primary_valid"],u.field.comparison["merged_valid"])
        self.assertTrue((u.path/"sic_only.csv").is_file())
        u.field.inspect_sic()
        u.root.update_idletasks()
        u.new_run()
        self.assertEqual(u.phase,"ready")

    def test_quiet_rf_stalled_iq_and_storage_health(self):
        u=self.ui
        def check_light(label, active=False):
            self.assertEqual(u.listening_light.cget("text"), f"●  {label}")
            self.assertEqual(u.listening_light.cget("style"),
                             "Listening.FleetCaption.TLabel" if active else "Idle.FleetCaption.TLabel")

        u.mode.set("Live RTL-SDR")
        check_light("Not listening")
        u.phase="running"
        u.stop=threading.Event()
        with patch("field_operations.time.monotonic", return_value=100.0) as clock:
            u.field.health_tick()
            check_light("Waiting for IQ")
            u.mail.put("iq", 100.)
            u.field.health_tick()
            check_light("Listening", active=True)
            self.assertIn("No trusted packet yet", u.field_health.cget("text"))
            u.snapshot={"last_valid_wall":79., "capture_s":25}
            u.field.health_tick()
            text=u.field_health.cget("text")
            self.assertIn("Last IQ",text)
            self.assertIn("Last trusted 21.",text)
            self.assertIn("IQ capacity",text)
            clock.return_value=101.5
            u.field.health_tick()
            check_light("Listening", active=True)
            clock.return_value=101.51
            u.field.health_tick()
            check_light("IQ stalled")
            self.assertIn("IQ stalled",u.field_health.cget("text"))
            u.mail.put("iq", 101.51)
            u.field.health_tick()
            check_light("Listening", active=True)
            u.stop.set()
            u.field.health_tick()
            check_light("Not listening")
        u.phase="error"
        u.journal=self.path/"packets.csv"
        u.snapshot={"capture_s":0}
        u.field.health_tick()
        self.assertIn("No IQ captured",u.field_health.cget("text"))
        check_light("Not listening")
        u.phase="ready"
        u.show_setup()
        u.setup.hide()
