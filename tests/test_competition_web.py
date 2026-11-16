import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient

from overseas_inventory.competition_web import app


ROOT = Path(__file__).resolve().parents[1]


class CompetitionWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.previous_runs_dir = os.environ.get("GLOBALPOOL_RUNS_DIR")
        os.environ["GLOBALPOOL_RUNS_DIR"] = self.tmpdir.name
        self.client = TestClient(app)

    def tearDown(self) -> None:
        if self.previous_runs_dir is None:
            os.environ.pop("GLOBALPOOL_RUNS_DIR", None)
        else:
            os.environ["GLOBALPOOL_RUNS_DIR"] = self.previous_runs_dir
        self.tmpdir.cleanup()

    def test_frontend_index_and_health(self) -> None:
        index = self.client.get("/")
        health = self.client.get("/student/health")

        self.assertEqual(index.status_code, 200)
        self.assertIn("<title>AEGIS-PREDICT", index.text)
        self.assertIn('id="login-form"', index.text)
        self.assertRegex(index.text, r'src="/assets/app\.js\?v=[a-f0-9]{12}"')
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["status"], "ok")

    def test_student_run_approval_and_execution(self) -> None:
        wrong_login = self.client.post("/student/login", json={"password": "错误密码"})
        self.assertEqual(wrong_login.status_code, 401)

        unauthorized = self.client.post("/student/runs", json={"horizon_days": 30})
        self.assertEqual(unauthorized.status_code, 401)

        login = self.client.post("/student/login", json={"password": "globalpool"})
        self.assertEqual(login.status_code, 200)
        self.assertTrue(login.json()["authenticated"])

        created = self.client.post("/student/runs", json={"horizon_days": 30})
        self.assertEqual(created.status_code, 200)
        payload = created.json()
        job_id = payload["metadata"]["job_id"]
        approval = payload["state"]["approvals"][0]

        fetched = self.client.get(f"/student/runs/{job_id}")
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["metadata"]["job_id"], job_id)

        decided = self.client.post(
            f"/student/runs/{job_id}/approvals/{approval['approval_id']}",
            json={"decision": "批准", "note": "student-test"},
        )
        self.assertEqual(decided.status_code, 200)
        self.assertEqual(decided.json()["state"]["approvals"][0]["status"], "批准")

        execution = self.client.post(
            f"/student/runs/{job_id}/execution",
            json={
                "sku": approval["sku"],
                "warehouse_id": approval["warehouse_id"],
                "executed_units": 10,
                "execution_status": "已下发",
                "note": "student-execution",
            },
        )
        self.assertEqual(execution.status_code, 200)
        self.assertEqual(execution.json()["state"]["execution_events"][0]["note"], "student-execution")

        started = self.client.post(
            f"/student/runs/{job_id}/simulation/start",
            json={"scenario": "black_friday", "steps": 1},
        )
        self.assertEqual(started.status_code, 200)
        self.assertTrue(started.json()["state"]["simulation"]["running"])

        ticked = self.client.post(
            f"/student/runs/{job_id}/simulation/tick",
            json={"scenario": "black_friday", "steps": 2},
        )
        self.assertEqual(ticked.status_code, 200)
        simulation = ticked.json()["state"]["simulation"]
        self.assertGreater(simulation["orders_total"], 0)
        self.assertTrue(simulation["warehouse_metrics"])

    def test_demo_simulation_run_is_created_from_engine(self) -> None:
        login = self.client.post("/student/login", json={"password": "globalpool"})
        self.assertEqual(login.status_code, 200)

        demo = self.client.post("/student/simulation/demo")

        self.assertEqual(demo.status_code, 200)
        self.assertEqual(demo.json()["metadata"]["data_source"], "realtime-simulation-demo")
        self.assertIn("simulation", demo.json()["state"])

    def test_competition_api_contracts_return_visual_page_data(self) -> None:
        login = self.client.post("/student/login", json={"password": "globalpool"})
        self.assertEqual(login.status_code, 200)

        dashboard = self.client.get("/api/dashboard")
        forecast = self.client.get("/api/forecast")
        replenishment = self.client.get("/api/replenishment")
        allocation = self.client.get("/api/allocation")
        comparison = self.client.get("/api/simulation/compare")

        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("metrics", dashboard.json())
        self.assertTrue(dashboard.json()["risk_ranking"])
        self.assertEqual(forecast.status_code, 200)
        self.assertTrue(forecast.json()["forecast"])
        self.assertEqual(replenishment.status_code, 200)
        self.assertTrue(replenishment.json()["recommendations"])
        self.assertEqual(allocation.status_code, 200)
        self.assertTrue(allocation.json()["allocations"])
        self.assertEqual(comparison.status_code, 200)
        self.assertIn("comparison", comparison.json())


if __name__ == "__main__":
    unittest.main()
