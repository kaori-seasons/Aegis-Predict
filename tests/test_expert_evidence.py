from copy import deepcopy
from datetime import date

import pytest

from aegis.core.errors import InvalidInput
from aegis.decision.analogues import evidence, month_end


def payload():
    clock = "2026-07-01T00:00:00+00:00"

    def revision(**kw):
        return {
            "source_system": "launches",
            "channel": "*",
            "revision": 1,
            "id": kw["sku"] + kw.get("event_kind", "profile"),
            "published_at": "2026-01-01T00:00:00+00:00",
            "received_at": "2026-01-01T00:00:00+00:00",
            "available_at": "2026-01-01T00:00:00+00:00",
            "is_deleted": False,
            **kw,
        }

    events = [
        revision(
            sku="NEW",
            event_kind="planned_launch",
            event_date="2026-07-03",
            cohort_key="basic",
            launch_group="target",
        ),
        revision(
            sku="NEW",
            event_kind="first_sale",
            event_date="2026-01-31",
            cohort_key="basic",
            launch_group="older-target-record",
        ),
        revision(
            sku="A",
            event_kind="first_sale",
            event_date="2026-01-31",
            cohort_key="basic",
            launch_group="peer",
        ),
        revision(
            sku="VAR",
            event_kind="first_sale",
            event_date="2026-01-31",
            cohort_key="basic",
            launch_group="target",
        ),
    ]
    profiles = [
        revision(
            sku=code,
            warehouse_id="W",
            positioning="basic",
            pricing_strategy="consumer market price",
            retail_price=20,
            currency="USD",
            price_basis="tax_inclusive",
            launch_plan="more colors",
            initial_supply_units=100,
            first_batch_scope="warehouse_channel",
        )
        for code in ("A", "NEW")
    ]
    start = date(2026, 1, 1)
    n = (date(2026, 7, 1) - start).days
    series = [
        {
            "sku": code,
            "warehouse_id": "W",
            "calendar_start": str(start),
            "timezone": "UTC",
            "daily_units": [2] * n,
            "missing_days": [False] * n,
            "stockout_days": [False] * n,
        }
        for code in ("NEW", "A", "VAR")
    ]
    return {
        "as_of": clock,
        "network": {
            "demand_history": series,
            "commerce_context": {
                "config": {
                    "launch_signal_source": "launches",
                    "launch_signal_channel": "*",
                    "launch_profile_source": "launches",
                },
                "sku_launches": events,
                "launch_profiles": profiles,
            },
            "procurement_constraints": [
                {"min_order_qty": 3000, "unit_purchase_cost": 12}
            ],
        },
        "ids": {"locations": {"W": "warehouse-id"}},
        "watermarks": {
            "authorities": [
                {"domain": "demand", "location_id": "warehouse-id", "channel": "*"}
            ]
        },
    }


def test_calendar_months_coverage_censoring_and_ratio_scope():
    p = payload()
    report = evidence(p, "NEW", "W", date(2026, 7, 3))
    card = report["analogs"][0]
    assert len(report["analogs"]) == 1 and card["sku"] == "A"
    assert (
        card["windows"]["D30"]["observed_sales"] == 60
        and card["d30_sales_to_initial_allocation"] == 0.6
    )
    assert card["windows"]["M3"]["end_exclusive"] == "2026-04-30"
    assert card["windows"]["M6"]["status"] == "immature"
    p["network"]["demand_history"][1]["stockout_days"][31] = True
    card = evidence(p, "NEW", "W", date(2026, 7, 3))["analogs"][0]
    assert (
        card["windows"]["D30"]["status"] == "censored_lower_bound"
        and card["d30_sales_to_initial_allocation"] is None
    )
    p["network"]["demand_history"][1]["stockout_days"][31] = False
    p["network"]["commerce_context"]["launch_profiles"][0]["first_batch_scope"] = (
        "sku_launch"
    )
    assert (
        evidence(p, "NEW", "W", date(2026, 7, 3))["analogs"][0][
            "d30_sales_to_initial_allocation"
        ]
        is None
    )
    assert month_end(date(2024, 1, 31), 1) == date(2024, 2, 29)


@pytest.mark.parametrize("clock", ["published_at", "received_at", "available_at"])
def test_future_revisions_no_leak_and_supply_blinding(clock):
    p = payload()
    before = evidence(p, "NEW", "W", date(2026, 7, 3))
    new = deepcopy(p["network"]["commerce_context"]["launch_profiles"][0])
    new.update(revision=2, retail_price=9999)
    new[clock] = "2026-07-02T00:00:00+00:00"
    p["network"]["commerce_context"]["launch_profiles"].append(new)
    after = evidence(p, "NEW", "W", date(2026, 7, 3))
    assert before["analogs"] == after["analogs"]
    assert "initial_supply_units" not in after["target"]["profile"]
    p["network"]["procurement_constraints"][0].update(
        min_order_qty=999999, unit_purchase_cost=999
    )
    changed = evidence(p, "NEW", "W", date(2026, 7, 3))
    assert (
        changed["target"] == before["target"]
        and changed["analogs"] == before["analogs"]
    )
    assert "procurement_constraints" not in changed


def test_incomplete_window_never_imputed_or_called_complete():
    p = payload()
    series = p["network"]["demand_history"][1]
    series["missing_days"][40] = True
    r = evidence(p, "NEW", "W", date(2026, 7, 3))["analogs"][0]["windows"]["D30"]
    assert r["status"] == "incomplete_coverage" and r["observed_sales"] == 58
    with pytest.raises(InvalidInput):
        evidence(p, "NEW", "W", date(2026, 7, 1))
