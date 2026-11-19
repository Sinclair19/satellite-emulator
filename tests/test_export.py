import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

EXPORT = Path(__file__).resolve().parents[1] / "tools/export_ns3.py"


class ExportTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.run_dir = Path(tmp.name)
        self.output = self.run_dir / "timeline.json"
        (self.run_dir / "sgp4-multi-snr-log.csv").write_text(
            "time_s,ue_id,active_satellite,distance_km,per_percent,ho_interrupting\n"
            "0,0,A,600,25,0\n0,1,X,1200,0,0\n1,0,A,600,100,1\n2,0,B,600,50,0\n"
            "3,0,B,600,100,1\n4,0,B,600,0,0\n"
        )
        self.handover = self.run_dir / "sgp4-handover-log.csv"
        self.handover.write_text(
            "time_s,ue_id,old_satellite,new_satellite,old_snr_dB,new_snr_dB,doppler_jump_kHz,old_dist_km,new_dist_km\n"
            "1.08,0,A,B,5,20,0,600,900\n3.07,0,B,C,,,FAILURE_NO_LINK\n"
        )
        self.detail = self.run_dir / "cho-detailed-log.csv"
        self.detail.write_text(
            "time_s,ue_id,event,source_cell,target_cell\n"
            "0.1,0,CHO_TRIGGERED,A,B\n1,0,EXECUTION_TRIGGERED,A,B\n"
            "3,0,EXECUTION_TRIGGERED,B,C\n"
        )

    def invoke(self, *args, success=True):
        p = subprocess.run(
            [
                sys.executable,
                EXPORT,
                self.run_dir,
                "--ue",
                "0",
                "--output",
                self.output,
                *args,
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(p.returncode == 0, success, p.stderr)
        if success:
            return {v["time"]: v for v in json.loads(self.output.read_text())}

    def test_measured_switch_failure_and_feeder(self):
        ranges = self.run_dir / "gateway.csv"
        ranges.write_text("time_s,satellite,feeder_km\n0,A,600\n0,B,900\n2.5,B,300\n")
        trace = self.invoke("--feeder-csv", ranges)
        self.assertEqual(trace[0]["pdr"], 0.75)
        self.assertAlmostEqual(trace[1.08]["delay_ms"], 1800 / 299.792458)
        self.assertAlmostEqual(trace[2.5]["delay_ms"], 900 / 299.792458)
        self.assertFalse(trace[1]["link_available"])
        self.assertEqual(trace[1.08]["satellite"], "B")
        self.assertFalse(trace[3]["link_available"])
        self.assertTrue(trace[3.07]["link_available"])
        self.assertEqual((trace[3.07]["satellite"], trace[3.07]["pdr"]), ("B", 0.5))
        self.assertNotIn(0.1, trace)

    def test_fallback_and_atomic_output(self):
        self.detail.unlink()
        self.invoke("--feeder-km", "400", success=False)
        trace = self.invoke("--feeder-km", "400", "--gap-ms", "80")
        self.assertFalse(trace[1]["link_available"])
        self.assertTrue(trace[1.08]["link_available"])
        before = self.output.read_bytes()
        self.invoke("--feeder-km", "-1", success=False)
        self.assertEqual(before, self.output.read_bytes())

    def test_channel_log_without_per_and_zero_gap(self):
        self.detail.unlink()
        (self.run_dir / "sgp4-multi-channel-log.csv").write_text(
            "time_s,ue_id,active_satellite,distance_km\n0,0,A,600\n2,0,B,900\n"
        )
        trace = self.invoke("--feeder-km", "500", "--gap-ms", "0", "--pdr", "1")
        self.assertTrue(
            all(e["link_available"] and e["pdr"] == 1 for e in trace.values())
        )
        self.assertEqual(trace[1.08]["satellite"], "B")


if __name__ == "__main__":
    unittest.main()
