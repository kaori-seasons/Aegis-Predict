"""Architecture smoke for new decision kernels; synthetic and inventory-read-only.

Also callable after verify_container.py to extend the SAME image's evidence record.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def smoke_code():
    return r"""
import json, uuid
from datetime import date, timedelta
from pathlib import Path
from aegis.decision.collaboration import allocate_total
from aegis.decision.forecasting import predict, required_horizons, monotone_intervals
from aegis.decision.protection import solve_protection
from aegis.planning.policy import PlanningPolicy
items=[dict(sku='A',warehouse_id='W',weight=1,pack_size=5),dict(sku='B',warehouse_id='W',weight=1,pack_size=7)]
assert [r['units'] for r in allocate_total(12,items,whole_packs=True)]==[5,7]
payload=json.loads(Path('/app/data/sample_network.json').read_text())
payload['forecast_as_of']='2026-11-17T00:00:00+00:00'
for series in payload['demand_history']:
    series['timezone']='UTC'
    series['calendar_start']=str(date(2026,11,17)-timedelta(days=len(series['daily_units'])))
policy=PlanningPolicy(forecast_model_version='rolling-champion-v3')
input_payload={'network':payload,'policy':policy.model_dump()}
horizons=required_horizons(input_payload,[7,30])
prediction=predict(input_payload,horizons,['mean_28','rolling-champion-v3'])
for row in prediction['rows']:
    assert len(row['daily'])==max(horizons) and min(row['daily'])>=0
    for q in ('p50','p80','p95'):
        totals=[row['horizons'][str(h)][q] for h in horizons]
        assert totals==sorted(totals)
policy=policy.model_copy(update={'algorithm_version':'aegis-protection-v3','forecast_result_id':str(uuid.uuid4())})
decision={'prediction':prediction,'arrivals':{},'forecast_id':'synthetic-container-smoke','execution_eligible':False}
result=solve_protection(payload,policy,decision)
assert result['algorithm_version']=='aegis-protection-v3'
assert result['optimizer']['status'] in ('optimal','feasible-time-limit','no-demand')
assert result['lines'] and result['summary']['execution_eligible'] is False
for line in result['lines']:
    route=next(r for r in line['route_requirements'] if r['mode']==line['selected_mode'])
    assert line['protection_days']==route['lead']+policy.review_period_days
    assert all(s['transport_mode']==line['selected_mode'] for s in line['shipments'])
print(json.dumps({'decision_kernel_passed':True,'series':len(payload['demand_history']),'horizons':horizons,'synthetic_only':True}))
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    record = json.loads(args.evidence.read_text())
    current_id = subprocess.check_output(
        ["docker", "image", "inspect", args.image, "--format", "{{.Id}}"], text=True
    ).strip()
    if record["image_id"] != current_id or record["platform"] != args.platform:
        raise SystemExit(
            "image changed: rerun full verify_container.py before extending evidence"
        )
    result = json.loads(
        subprocess.check_output(
            [
                "docker",
                "run",
                "--rm",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=64m",
                "--platform",
                args.platform,
                args.image,
                "python",
                "-c",
                smoke_code(),
            ],
            text=True,
        )
    )
    record.update(decision_kernel_passed=True, decision_smoke=result)
    args.evidence.write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
