from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from overseas_inventory.webapp import (
    append_execution_event,
    create_simulation_run,
    load_run_state,
    start_simulation,
    tick_simulation,
    safe_redirect_path,
    sign_session,
    update_approval_state,
    verify_session,
)


ROOT = Path(__file__).resolve().parents[1]


class WebAppTests(unittest.TestCase):
    def test_create_simulation_run_generates_result_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = create_simulation_run(runs_dir=Path(tmpdir), horizon_days=30)
            job_dir = result["job_dir"]

            self.assertTrue((job_dir / "dashboard.html").exists())
            self.assertTrue((job_dir / "replenishment_plan.md").exists())
            self.assertTrue((job_dir / "replenishment_plan.json").exists())
            self.assertTrue((job_dir / "operations_state.json").exists())
            self.assertEqual(result["metadata"]["recommendation_count"], 60)
            self.assertEqual(result["metadata"]["data_source"], "simulation-engine")

    def test_run_state_tracks_approval_and_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = create_simulation_run(runs_dir=Path(tmpdir), horizon_days=30)
            job_dir = result["job_dir"]
            state = load_run_state(job_dir)
            approval_id = state["approvals"][0]["approval_id"]

            approval = update_approval_state(job_dir, approval_id, "批准", actor="admin", note="同意补货")
            event = append_execution_event(
                job_dir,
                actor="admin",
                sku=approval["sku"],
                warehouse_id=approval["warehouse_id"],
                executed_units=10,
                execution_status="已下发",
                note="PO-001",
            )
            updated = load_run_state(job_dir)

            self.assertEqual(approval["status"], "批准")
            self.assertEqual(updated["approvals"][0]["note"], "同意补货")
            self.assertEqual(event["execution_status"], "已下发")
            self.assertEqual(updated["execution_events"][0]["note"], "PO-001")

    def test_realtime_simulation_generates_orders_and_dynamic_recommendations(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = create_simulation_run(runs_dir=Path(tmpdir), horizon_days=30)
            job_dir = result["job_dir"]

            started = start_simulation(job_dir, scenario="us_spike")
            ticked = tick_simulation(job_dir, steps=3, scenario="us_spike")
            updated = load_run_state(job_dir)["simulation"]

            self.assertTrue(started["running"])
            self.assertEqual(ticked["scenario"], "us_spike")
            self.assertGreater(updated["orders_total"], 0)
            self.assertGreater(updated["units_requested"], 0)
            self.assertIn("market_pulse", updated)
            self.assertIn("platform_mix", updated["market_pulse"])
            self.assertIn("units_cancelled", updated)
            self.assertEqual(len(updated["time_series"]), 3)
            self.assertIn("fill_rate", updated["time_series"][-1])
            self.assertIn("traffic_index", updated["time_series"][-1])
            self.assertTrue(updated["warehouse_metrics"])
            self.assertTrue(updated["dynamic_recommendations"])
            self.assertEqual(updated["current_sim_day"], 181)
            self.assertEqual(updated["simulation_clock"], "D181 03:00")

    def test_simulation_rolls_forecast_decisions_and_inbound_shipments(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = create_simulation_run(runs_dir=Path(tmpdir), horizon_days=30)
            job_dir = result["job_dir"]

            start_simulation(job_dir, scenario="us_spike")
            after_decision = tick_simulation(job_dir, steps=6, scenario="us_spike")
            self.assertEqual(after_decision["decision_version"], 2)
            self.assertGreater(len(after_decision["in_transit_shipments"]), 0)
            self.assertIn("eta_confidence", after_decision["in_transit_shipments"][0])
            self.assertIn("delay_reason", after_decision["in_transit_shipments"][0])
            self.assertEqual(after_decision["next_decision_in_hours"], 6)

            after_day = tick_simulation(job_dir, steps=12, scenario="us_spike")
            after_day = tick_simulation(job_dir, steps=6, scenario="us_spike")
            self.assertEqual(after_day["forecast_version"], 2)
            self.assertEqual(after_day["simulation_clock"], "D182 00:00")
            self.assertTrue(after_day["latest_forecast_summary"])

    def test_signed_session_verification(self) -> None:
        token = sign_session("admin")

        self.assertTrue(verify_session(token))
        self.assertFalse(verify_session(token + "tampered"))

    def test_safe_redirect_path_rejects_external_url(self) -> None:
        self.assertEqual(safe_redirect_path("/app/runs/abc"), "/app/runs/abc")
        self.assertEqual(safe_redirect_path("//example.com"), "/")


if __name__ == "__main__":
    unittest.main()
