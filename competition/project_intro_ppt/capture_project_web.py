from __future__ import annotations

import json
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_URL = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parent
SHOT_DIR = ROOT / "images" / "web"
SHOT_DIR.mkdir(parents=True, exist_ok=True)

SCENARIO = "black_friday"
SIM_STEPS = 30
TICK_BATCH = 10
VIEWPORT = {"width": 1440, "height": 1000}


def api_fetch(page, path: str):
    return page.evaluate(
        """async (path) => {
            const response = await fetch(path, { credentials: 'same-origin' });
            if (!response.ok) throw new Error(`${response.status} ${path}`);
            return await response.json();
        }""",
        path,
    )


def api_post(page, path: str, payload: dict | None = None):
    return page.evaluate(
        """async ({path, payload}) => {
            const response = await fetch(path, {
                method: 'POST',
                credentials: 'same-origin',
                headers: payload ? { 'Content-Type': 'application/json' } : undefined,
                body: payload ? JSON.stringify(payload) : undefined,
            });
            if (!response.ok) throw new Error(`${response.status} ${path}`);
            return await response.json();
        }""",
        {"path": path, "payload": payload},
    )


def screenshot_page(page, page_id: str, name: str):
    page.locator(f'.page-tab[data-page="{page_id}"]').click()
    page.wait_for_timeout(500)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(300)
    page.screenshot(path=str(SHOT_DIR / name), full_page=False)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(viewport=VIEWPORT, device_scale_factor=1)
    page = context.new_page()
    page.goto(BASE_URL, wait_until="networkidle")

    if page.locator("#login-panel:not(.hidden)").count():
        page.fill("#password", "globalpool")
        page.locator('#login-form button[type="submit"]').click()

    page.wait_for_selector("#workspace:not(.hidden)", timeout=180_000)
    page.wait_for_load_state("networkidle")
    page.evaluate("() => window.setSimulationTimer && window.setSimulationTimer('', false, 'normal')")

    job_text = page.locator("#job-id").inner_text(timeout=30_000)
    match = re.search(r"RUN\s+(.+)", job_text, re.IGNORECASE)
    if not match:
        raise RuntimeError(f"Cannot parse job id from: {job_text}")
    job_id = match.group(1).strip()

    api_post(page, f"/student/runs/{job_id}/simulation/reset")
    api_post(page, f"/student/runs/{job_id}/simulation/start", {"scenario": SCENARIO, "steps": 1})
    current_run = None
    remaining_steps = SIM_STEPS
    while remaining_steps > 0:
        steps = min(TICK_BATCH, remaining_steps)
        current_run = api_post(page, f"/student/runs/{job_id}/simulation/tick", {"scenario": SCENARIO, "steps": steps})
        remaining_steps -= steps
    current_run = api_post(page, f"/student/runs/{job_id}/simulation/stop")
    page.evaluate("run => window.renderRun(run)", current_run)
    page.wait_for_timeout(800)

    screenshot_page(page, "overview-page", "01-web-pool.png")
    screenshot_page(page, "forecast-page", "02-web-forecast.png")
    screenshot_page(page, "replenishment-page", "03-web-replenishment.png")
    screenshot_page(page, "allocation-page", "04-web-allocation.png")
    screenshot_page(page, "evaluation-page", "05-web-simulation.png")

    dashboard = api_fetch(page, f"/api/dashboard?job_id={job_id}")
    forecast = api_fetch(page, f"/api/forecast?job_id={job_id}")
    replenishment = api_fetch(page, f"/api/replenishment?job_id={job_id}")
    allocation = api_fetch(page, f"/api/allocation?job_id={job_id}")
    compare = api_fetch(page, f"/api/simulation/compare?job_id={job_id}")
    realtime_orders = api_fetch(page, f"/api/orders/realtime?job_id={job_id}")
    run_payload = api_fetch(page, f"/student/runs/{job_id}")

    dom_summary = page.evaluate(
        """() => ({
            title: document.querySelector('#run-title')?.innerText || '',
            story: document.querySelector('#story-strip')?.innerText || '',
            metrics: document.querySelector('#metrics')?.innerText || '',
            clock: document.querySelector('#sim-clock')?.innerText || '',
            activePage: document.querySelector('.page-tab.active')?.innerText || '',
        })"""
    )

    metrics = {
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "base_url": BASE_URL,
        "job_id": job_id,
        "scenario": SCENARIO,
        "sim_steps": SIM_STEPS,
        "dashboard": dashboard,
        "forecast": forecast,
        "replenishment": replenishment,
        "allocation": allocation,
        "simulation_compare": compare,
        "realtime_orders": realtime_orders,
        "run_metadata": run_payload.get("metadata", {}),
        "run_state": run_payload.get("state", {}),
        "plan_summary": {
            "recommendation_count": len(run_payload.get("plan", {}).get("recommendations", [])),
            "allocation_count": len(run_payload.get("plan", {}).get("allocations", [])),
            "warehouse_count": len(run_payload.get("plan", {}).get("warehouse_map", [])),
            "sku_count": len({r.get("sku") for r in run_payload.get("plan", {}).get("recommendations", [])}),
            "model_pipeline": run_payload.get("plan", {}).get("model_pipeline", {}),
        },
        "dom_summary": dom_summary,
        "screenshots": {
            "pool": "images/web/01-web-pool.png",
            "forecast": "images/web/02-web-forecast.png",
            "replenishment": "images/web/03-web-replenishment.png",
            "allocation": "images/web/04-web-allocation.png",
            "simulation": "images/web/05-web-simulation.png",
        },
    }
    (ROOT / "project_metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    browser.close()

print(json.dumps({"job_id": job_id, "metrics": str(ROOT / "project_metrics.json"), "screenshots": str(SHOT_DIR)}, ensure_ascii=False))
