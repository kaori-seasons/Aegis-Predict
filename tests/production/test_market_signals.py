from copy import deepcopy
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
import random
import pytest
from overseas_inventory.models import DemandSeries
from aegis.planning.forecast import forecast, MARKET_MODEL_VERSION
from aegis.planning.market_forecast import FEATURES, asof_signals, origin_at
from aegis.planning.backtest import rolling_backtest


def fixture(n=168):
    start = date(2026, 1, 1)
    rng = random.Random(71)
    promotion = [rng.random() < 0.4 for _ in range(n + 35)]
    series = DemandSeries(
        "S",
        "W",
        [30 + (18 if x else 0) for x in promotion[:n]],
        calendar_start=str(start),
    )
    revisions = []
    for i, event in enumerate(promotion):
        day = start + timedelta(days=i)
        published = datetime.combine(day - timedelta(days=45), time(12), UTC)
        revisions.append(
            {
                "id": str(i),
                "sku": "S",
                "warehouse_id": "W",
                "source_system": "market:test",
                "platform": "amazon",
                "signal_kind": "planned",
                "signal_date": str(day),
                "revision": 1,
                "published_at": published.isoformat(),
                "received_at": published.isoformat(),
                "available_at": published.isoformat(),
                "is_deleted": False,
                **{k: 1.0 for k in FEATURES},
                "promotion_lift": 1.8 if event else 1.0,
            }
        )
    return series, revisions


def net(s, revisions):
    return SimpleNamespace(
        demand_history=[s],
        skus={"S": SimpleNamespace(category="c")},
        market_signal_revisions=revisions,
    )


def predict(s, rows, **kwargs):
    return forecast(
        s, 7, model_version=MARKET_MODEL_VERSION, market_revisions=rows, **kwargs
    )


def test_market_candidate_learns_only_training_and_improves_independent_holdouts():
    s, rows = fixture()
    r = rolling_backtest(
        net(s, rows), model_version=MARKET_MODEL_VERSION, max_folds_per_series=4
    )
    assert len(r["models"]) == 13 and r["primary_model"] == "market_adaptive"
    assert r["market_comparison"]["market_adaptive"]["skill"] > 0.3
    assert all(
        m["overall"]["folds"] == r["metrics"]["overall"]["folds"]
        for m in r["models"].values()
    )
    for f in r["folds"]:
        for inner in f["market_selection"].get("inner_market_evidence", []):
            assert inner["end_exclusive"] <= f["origin"]
            assert inner["as_of"] == origin_at(s, inner["origin"]).isoformat()
    poisoned = deepcopy(s)
    poisoned.daily_units[140:] = [100000] * 28
    other = rolling_backtest(
        net(poisoned, rows), model_version=MARKET_MODEL_VERSION, max_folds_per_series=4
    )
    a = next(f for f in r["folds"] if f["origin"] == 140)
    b = next(f for f in other["folds"] if f["origin"] == 140)
    assert a["market_selection"] == b["market_selection"]
    assert (
        a["predictions"]["market_adaptive"]["prediction"]
        == b["predictions"]["market_adaptive"]["prediction"]
    )


@pytest.mark.parametrize(
    "timestamp_key", ["published_at", "received_at", "available_at"]
)
def test_later_publication_receipt_or_availability_cannot_change_prediction(
    timestamp_key,
):
    s, rows = fixture()
    clock = origin_at(s)
    baseline = predict(s, rows).to_dict()
    changes = []
    for row in rows:
        change = row | {
            "id": row["id"] + "-late",
            "revision": 2,
            "promotion_lift": 99999.0,
        }
        change[timestamp_key] = (clock + timedelta(days=1)).isoformat()
        if timestamp_key == "received_at":
            change["available_at"] = change[timestamp_key]
        changes.append(change)
    assert predict(s, rows + changes).to_dict() == baseline


def test_realized_future_metrics_and_other_series_are_never_covariates():
    s, rows = fixture()
    baseline = predict(s, rows).to_dict()
    extras = [
        r
        | {
            "id": "observed-" + r["id"],
            "signal_kind": "observed",
            "promotion_lift": 99999.0,
        }
        for r in rows
    ]
    extras += [r | {"sku": "OTHER", "promotion_lift": 99999.0} for r in rows]
    assert predict(s, rows + extras).to_dict() == baseline


def test_out_of_order_revisions_tombstones_and_provider_ambiguity():
    s, rows = fixture()
    clock = origin_at(s)
    row = rows[-40]
    correction = row | {"id": "corrected", "revision": 2, "promotion_lift": 2.2}
    deleted = correction | {"id": "deleted", "revision": 3, "is_deleted": True}
    selected, _ = asof_signals(s, [deleted, correction, row], clock)
    assert date.fromisoformat(row["signal_date"]) not in selected
    selected, _ = asof_signals(s, [correction, row], clock)
    assert selected[date.fromisoformat(row["signal_date"])]["revision"] == 2
    with pytest.raises(ValueError, match="provider/platform"):
        predict(s, rows + [row | {"source_system": "another-provider"}])


def test_missing_metadata_late_backfill_and_no_calendar_fall_back_exactly():
    s, rows = fixture()
    clock = origin_at(s)
    late = [
        r
        | {
            "received_at": (clock + timedelta(days=1)).isoformat(),
            "available_at": (clock + timedelta(days=1)).isoformat(),
        }
        for r in rows
    ]
    unknown = [{k: v for k, v in r.items() if k != "available_at"} for r in rows]
    for signals in ([], late, unknown):
        a = predict(s, signals)
        b = forecast(s, 7)
        assert a.daily_rate == b.daily_rate and not a.selection["market_enabled"]
    undated = DemandSeries("S", "W", s.daily_units)
    assert predict(undated, rows).daily_rate == forecast(undated, 7).daily_rate
    with pytest.raises(ValueError, match="precedes"):
        predict(s, rows, as_of=clock - timedelta(days=1))


def test_future_covariates_can_affect_forecast_only_when_already_known():
    s, rows = fixture()
    base = predict(s, rows, model="market_ridge")
    changed = [
        r | {"promotion_lift": 2.5}
        if date.fromisoformat(r["signal_date"])
        >= date.fromisoformat(s.calendar_start) + timedelta(days=len(s.daily_units))
        else r
        for r in rows
    ]
    assert predict(s, changed, model="market_ridge").daily_rate > base.daily_rate
    # Archived models ignore the entire market stream and preserve their numeric path.
    assert (
        forecast(s, 7, market_revisions=changed).to_dict()
        == forecast(s, 7, market_revisions=rows).to_dict()
    )


def test_market_origin_uses_local_calendar_and_dst_not_utc_day_offsets():
    s = DemandSeries(
        "S", "W", [10] * 7, calendar_start="2026-03-07", timezone="America/New_York"
    )
    assert origin_at(s, 1).isoformat() == "2026-03-08T05:00:00+00:00"
    assert origin_at(s, 2).isoformat() == "2026-03-09T04:00:00+00:00"
    from aegis.integration.quality import QualityChecker
    from uuid import uuid4

    record = {
        "platform": "amazon",
        "signal_kind": "observed",
        "revision": 1,
        "date": date(2026, 1, 1),
        "published_at": datetime(2026, 1, 1, 15, tzinfo=UTC),
        **{k: 1.0 for k in FEATURES},
    }
    checker = QualityChecker(
        SimpleNamespace(get=lambda *args: SimpleNamespace(timezone="Asia/Tokyo")),
        uuid4(),
        now=datetime(2026, 2, 1, tzinfo=UTC),
    )
    assert checker.check("market_signals", record, {"location_id": uuid4()}) is None
    checker.session = SimpleNamespace(
        get=lambda *args: SimpleNamespace(timezone="America/Los_Angeles")
    )
    record["published_at"] = datetime(2026, 1, 2, 7, 59, tzinfo=UTC)
    assert (
        checker.check("market_signals", record, {"location_id": uuid4()}).code
        == "invalid_observed_publication"
    )
