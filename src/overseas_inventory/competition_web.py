from __future__ import annotations

import argparse
import hmac
import json
import os
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .webapp import (
    DEFAULT_RUNS_DIR,
    DEFAULT_SAMPLE_PATH,
    OperationError,
    append_execution_event,
    admin_password,
    clamp_int,
    create_simulation_run,
    load_run_bundle,
    load_run_state,
    reset_simulation,
    sign_session,
    SimulationDataError,
    start_simulation,
    stop_simulation,
    schema_summary,
    tick_simulation,
    update_approval_state,
    verify_session,
)


SIMULATION_DEMO_SOURCE = "realtime-simulation-demo"


FRONTEND_DIR = Path(__file__).resolve().parent / "web_frontend"
ASSETS_DIR = FRONTEND_DIR / "assets"


class ApprovalInput(BaseModel):
    decision: str = Field(..., examples=["批准"])
    note: str = ""


class ExecutionInput(BaseModel):
    sku: str
    warehouse_id: str
    executed_units: int = Field(..., gt=0)
    execution_status: str = "已下发"
    note: str = ""


class LoginInput(BaseModel):
    password: str


class SimulationInput(BaseModel):
    scenario: str = "normal"
    steps: int = Field(1, ge=1, le=12)


class RunInput(BaseModel):
    horizon_days: int = Field(30, ge=7, le=90)


def runs_dir() -> Path:
    return Path(os.environ.get("GLOBALPOOL_RUNS_DIR", str(DEFAULT_RUNS_DIR)))


def current_user(request: Request) -> str | None:
    session = request.cookies.get("globalpool_session", "")
    return "admin" if verify_session(session) else None


def require_session(request: Request) -> str:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录比赛控制台。")
    return user


app = FastAPI(
    title="GlobalPool AI Student Web",
    version="0.3.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/student/health")
def health() -> dict:
    return {"status": "ok", "service": "GlobalPool AI Student Web"}


@app.get("/student/session")
def session_status(request: Request) -> dict:
    return {"authenticated": current_user(request) is not None}


@app.post("/student/login")
def login(payload: LoginInput, response: Response) -> dict:
    if not hmac.compare_digest(payload.password.encode("utf-8"), admin_password().encode("utf-8")):
        raise HTTPException(status_code=401, detail="密码不正确。")
    response.set_cookie(
        "globalpool_session",
        sign_session("admin"),
        httponly=True,
        samesite="lax",
        path="/",
    )
    return {"authenticated": True}


@app.post("/student/logout")
def logout(response: Response) -> dict:
    response.delete_cookie("globalpool_session", path="/")
    return {"authenticated": False}


@app.get("/student/schema")
def schema() -> dict:
    return schema_summary()


@app.get("/student/sample")
def sample() -> FileResponse:
    return FileResponse(DEFAULT_SAMPLE_PATH, filename="sample_network.json")


@app.get("/student/runs")
def list_runs(_: Annotated[str, Depends(require_session)], limit: int = 20) -> dict:
    root = runs_dir()
    root.mkdir(parents=True, exist_ok=True)
    rows = []
    for job_dir in sorted((item for item in root.iterdir() if item.is_dir()), reverse=True)[: max(1, min(limit, 100))]:
        metadata_path = job_dir / "metadata.json"
        if metadata_path.exists():
            rows.append(json.loads(metadata_path.read_text(encoding="utf-8")))
    return {"runs": rows}


@app.post("/student/runs")
def create_student_run(
    _: Annotated[str, Depends(require_session)],
    payload: RunInput | None = None,
) -> dict:
    horizon_days = clamp_int(str(payload.horizon_days if payload else 30), 7, 90, 30)
    try:
        result = create_simulation_run(
            runs_dir=runs_dir(),
            horizon_days=horizon_days,
        )
    except SimulationDataError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return build_run_payload(result["metadata"]["job_id"])


@app.get("/student/runs/{job_id}")
def get_student_run(job_id: str, _: Annotated[str, Depends(require_session)]) -> dict:
    return build_run_payload(job_id)


@app.post("/student/simulation/demo")
def get_or_create_simulation_demo(_: Annotated[str, Depends(require_session)]) -> dict:
    root = runs_dir()
    root.mkdir(parents=True, exist_ok=True)
    for job_dir in sorted((item for item in root.iterdir() if item.is_dir()), reverse=True):
        metadata_path = job_dir / "metadata.json"
        if not metadata_path.exists():
            continue
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("data_source") == SIMULATION_DEMO_SOURCE:
            return build_run_payload(metadata["job_id"])

    try:
        result = create_simulation_run(
            runs_dir=root,
            horizon_days=30,
            label=SIMULATION_DEMO_SOURCE,
        )
    except SimulationDataError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return build_run_payload(result["metadata"]["job_id"])


@app.post("/student/runs/{job_id}/approvals/{approval_id}")
def decide_approval(
    job_id: str,
    approval_id: str,
    payload: ApprovalInput,
    _: Annotated[str, Depends(require_session)],
) -> dict:
    bundle = load_bundle_or_404(job_id)
    try:
        update_approval_state(
            bundle["job_dir"],
            approval_id=approval_id,
            decision=payload.decision,
            actor="student-web",
            note=payload.note,
        )
    except OperationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return build_run_payload(job_id)


@app.post("/student/runs/{job_id}/execution")
def write_execution(
    job_id: str,
    payload: ExecutionInput,
    _: Annotated[str, Depends(require_session)],
) -> dict:
    bundle = load_bundle_or_404(job_id)
    try:
        append_execution_event(
            bundle["job_dir"],
            actor="student-web",
            sku=payload.sku,
            warehouse_id=payload.warehouse_id,
            executed_units=payload.executed_units,
            execution_status=payload.execution_status,
            note=payload.note,
        )
    except OperationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return build_run_payload(job_id)


@app.post("/student/runs/{job_id}/simulation/start")
def start_run_simulation(
    job_id: str,
    payload: SimulationInput,
    _: Annotated[str, Depends(require_session)],
) -> dict:
    bundle = load_bundle_or_404(job_id)
    start_simulation(bundle["job_dir"], scenario=payload.scenario)
    return build_run_payload(job_id)


@app.post("/student/runs/{job_id}/simulation/stop")
def stop_run_simulation(job_id: str, _: Annotated[str, Depends(require_session)]) -> dict:
    bundle = load_bundle_or_404(job_id)
    stop_simulation(bundle["job_dir"])
    return build_run_payload(job_id)


@app.post("/student/runs/{job_id}/simulation/reset")
def reset_run_simulation(job_id: str, _: Annotated[str, Depends(require_session)]) -> dict:
    bundle = load_bundle_or_404(job_id)
    reset_simulation(bundle["job_dir"])
    return build_run_payload(job_id)


@app.post("/student/runs/{job_id}/simulation/tick")
def tick_run_simulation(
    job_id: str,
    payload: SimulationInput,
    _: Annotated[str, Depends(require_session)],
) -> dict:
    bundle = load_bundle_or_404(job_id)
    tick_simulation(bundle["job_dir"], steps=payload.steps, scenario=payload.scenario)
    return build_run_payload(job_id)


@app.get("/student/runs/{job_id}/files/{filename}")
def run_file(job_id: str, filename: str, _: Annotated[str, Depends(require_session)]) -> FileResponse:
    if filename not in {"dashboard.html", "replenishment_plan.md", "replenishment_plan.json", "operations_state.json"}:
        raise HTTPException(status_code=403, detail="File is not allowed.")
    bundle = load_bundle_or_404(job_id)
    path = bundle["job_dir"] / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(path)


@app.get("/api/dashboard")
def api_dashboard(_: Annotated[str, Depends(require_session)], job_id: str | None = None) -> dict:
    bundle = api_bundle(job_id)
    plan = bundle["plan"]
    state = load_run_state(bundle["job_dir"])
    simulation = state.get("simulation", {})
    recommendations = plan.get("recommendations", [])
    inventory = simulation.get("inventory", {})
    total_inventory = sum(int(value) for value in inventory.values()) if inventory else sum(
        int(row.get("inventory_position", 0)) for row in recommendations
    )
    demand_30d = sum(int(row.get("p80_units", 0)) for row in recommendations)
    daily_demand = sum(float(row.get("daily_rate", 0)) for row in recommendations)
    risk_rows = sorted(
        recommendations,
        key=lambda row: (float(row.get("urgency", 0)), int(row.get("recommended_units", 0))),
        reverse=True,
    )[:8]
    return {
        "metrics": {
            "total_inventory": total_inventory,
            "risk_sku_count": sum(1 for row in recommendations if float(row.get("urgency", 0)) >= 0.55),
            "demand_30d": demand_30d,
            "fill_rate": simulation.get("fill_rate", plan.get("simulation_comparison", {}).get("intelligent", {}).get("fill_rate")),
            "turnover_days": round(total_inventory / max(1.0, daily_demand), 1),
            "today_replenishment_units": sum(int(row.get("recommended_units", 0)) for row in recommendations),
        },
        "warehouses": plan.get("warehouse_map", []),
        "risk_ranking": risk_rows,
        "orders": simulation.get("recent_orders", []),
        "market_pulse": simulation.get("market_pulse", {}),
    }


@app.get("/api/warehouses")
def api_warehouses(_: Annotated[str, Depends(require_session)], job_id: str | None = None) -> dict:
    bundle = api_bundle(job_id)
    state = load_run_state(bundle["job_dir"])
    return {"warehouses": state.get("simulation", {}).get("warehouse_metrics") or bundle["plan"].get("warehouse_map", [])}


@app.get("/api/orders/realtime")
def api_realtime_orders(_: Annotated[str, Depends(require_session)], job_id: str | None = None) -> dict:
    bundle = api_bundle(job_id)
    state = load_run_state(bundle["job_dir"])
    simulation = state.get("simulation", {})
    return {"orders": simulation.get("recent_orders", []), "market_pulse": simulation.get("market_pulse", {})}


@app.get("/api/forecast")
def api_forecast(
    _: Annotated[str, Depends(require_session)],
    job_id: str | None = None,
    sku: str | None = None,
    warehouse_id: str | None = None,
) -> dict:
    bundle = api_bundle(job_id)
    rows = bundle["plan"].get("recommendations", [])
    if sku:
        rows = [row for row in rows if row.get("sku") == sku]
    if warehouse_id:
        rows = [row for row in rows if row.get("warehouse_id") == warehouse_id]
    state = load_run_state(bundle["job_dir"])
    return {
        "model_pipeline": bundle["plan"].get("model_pipeline", {}),
        "forecast": rows,
        "rolling_summary": state.get("simulation", {}).get("latest_forecast_summary", []),
    }


@app.get("/api/replenishment")
def api_replenishment(_: Annotated[str, Depends(require_session)], job_id: str | None = None) -> dict:
    bundle = api_bundle(job_id)
    state = load_run_state(bundle["job_dir"])
    return {
        "recommendations": bundle["plan"].get("recommendations", []),
        "approvals": state.get("approvals", []),
        "in_transit_shipments": state.get("simulation", {}).get("in_transit_shipments", []),
    }


@app.get("/api/allocation")
def api_allocation(_: Annotated[str, Depends(require_session)], job_id: str | None = None) -> dict:
    bundle = api_bundle(job_id)
    return {
        "allocations": bundle["plan"].get("allocations", []),
        "pool_levels": bundle["plan"].get("pool_levels", []),
        "constraint_summary": bundle["plan"].get("constraint_summary", {}),
    }


@app.get("/api/simulation/compare")
def api_simulation_compare(_: Annotated[str, Depends(require_session)], job_id: str | None = None) -> dict:
    bundle = api_bundle(job_id)
    state = load_run_state(bundle["job_dir"])
    return {
        "comparison": bundle["plan"].get("simulation_comparison", {}),
        "simulation": state.get("simulation", {}),
    }


def build_run_payload(job_id: str) -> dict:
    bundle = load_bundle_or_404(job_id)
    return {
        "metadata": bundle["metadata"],
        "plan": bundle["plan"],
        "state": load_run_state(bundle["job_dir"]),
    }


def load_bundle_or_404(job_id: str) -> dict:
    try:
        return load_run_bundle(runs_dir(), job_id)
    except OperationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def api_bundle(job_id: str | None = None) -> dict:
    if job_id:
        return load_bundle_or_404(job_id)
    root = runs_dir()
    root.mkdir(parents=True, exist_ok=True)
    candidate_metadata = []
    for job_dir in sorted((item for item in root.iterdir() if item.is_dir()), reverse=True):
        metadata_path = job_dir / "metadata.json"
        if metadata_path.exists():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            candidate_metadata.append(metadata)
            if metadata.get("data_source") == SIMULATION_DEMO_SOURCE:
                return load_bundle_or_404(metadata["job_id"])
    if candidate_metadata:
        return load_bundle_or_404(candidate_metadata[0]["job_id"])
    try:
        result = create_simulation_run(
            runs_dir=root,
            horizon_days=30,
            label=SIMULATION_DEMO_SOURCE,
        )
    except SimulationDataError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return load_bundle_or_404(result["metadata"]["job_id"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the GlobalPool AI competition web app.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    import uvicorn

    uvicorn.run("overseas_inventory.competition_web:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
