from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from overseas_inventory.allocation import allocate_from_global_pool
from overseas_inventory.demo import build_display_names
from overseas_inventory.forecasting import forecast_all, forecast_demand
from overseas_inventory.models import DemandSeries, MarketSignal, load_network
from overseas_inventory.replenishment import build_replenishment_recommendations, round_up_to_pack
from overseas_inventory.simulation import compare_strategies


ROOT = Path(__file__).resolve().parents[1]


class ForecastingTests(unittest.TestCase):
    def test_forecast_tracks_recent_growth(self) -> None:
        series = DemandSeries(
            sku="SKU-X",
            warehouse_id="US-LAX",
            daily_units=[2, 2, 3, 3, 5, 6, 7, 8],
        )

        result = forecast_demand(series, horizon_days=30)

        self.assertGreater(result.daily_rate, 4.5)
        self.assertGreater(result.horizon_units, 135)
        self.assertGreaterEqual(result.confidence, 0.35)
        self.assertIn("ensemble", result.model_name)

    def test_market_signal_lifts_forecast(self) -> None:
        series = DemandSeries(
            sku="SKU-X",
            warehouse_id="US-LAX",
            daily_units=[5, 5, 6, 6, 6, 7, 7, 8],
        )
        signal = MarketSignal(
            sku="SKU-X",
            warehouse_id="US-LAX",
            platform="Amazon",
            ad_spend_index=1.3,
            promotion_lift=1.25,
            seasonal_index=1.15,
            platform_event_lift=1.2,
            price_index=0.98,
        )

        without_signal = forecast_demand(series, horizon_days=30)
        with_signal = forecast_demand(series, horizon_days=30, signal=signal)

        self.assertGreater(with_signal.daily_rate, without_signal.daily_rate)
        self.assertGreater(with_signal.signal_lift, 0)

    def test_sample_network_uses_ml_tree_ensemble(self) -> None:
        network = load_network(ROOT / "data/sample_network.json")

        forecasts = forecast_all(network.demand_history, horizon_days=30, signals=network.market_signals)

        self.assertEqual(len(forecasts), 60)
        self.assertIn("LightGBM/XGBoost/RandomForest ML ensemble", forecasts[0].model_name)
        self.assertIn("trained_samples=", forecasts[0].feature_notes)
        self.assertIn("active_models=", forecasts[0].feature_notes)


class ReplenishmentTests(unittest.TestCase):
    def test_sample_network_is_3c_accessory_scenario(self) -> None:
        network = load_network(ROOT / "data/sample_network.json")

        self.assertEqual(len(network.skus), 15)
        self.assertEqual(len(network.warehouses), 4)
        self.assertEqual(len(network.demand_history), 60)
        self.assertTrue(all(len(series.daily_units) == 180 for series in network.demand_history))
        self.assertIn("HUB-7IN1", network.skus)
        self.assertIn("US-LAX", network.warehouses)

    def test_sample_network_has_human_readable_display_names(self) -> None:
        network = load_network(ROOT / "data/sample_network.json")

        display_names = build_display_names(network)

        self.assertEqual(display_names["skus"]["HUB-7IN1"], "USB-C 拓展坞 7 合 1")
        self.assertEqual(display_names["warehouses"]["AU-SYD"], "澳洲悉尼仓")

    def test_round_up_to_pack(self) -> None:
        self.assertEqual(round_up_to_pack(1, 10), 10)
        self.assertEqual(round_up_to_pack(20, 10), 20)
        self.assertEqual(round_up_to_pack(0, 10), 0)

    def test_allocation_never_exceeds_pool_or_need(self) -> None:
        network = load_network(ROOT / "data/sample_network.json")
        recommendations = build_replenishment_recommendations(network, horizon_days=30)
        allocations = allocate_from_global_pool(network, recommendations)

        allocated_by_sku: dict[str, int] = {}
        need_by_key = {(item.sku, item.warehouse_id): item.recommended_units for item in recommendations}
        for decision in allocations:
            allocated_by_sku[decision.sku] = allocated_by_sku.get(decision.sku, 0) + decision.allocated_units
            self.assertTrue(set(decision.transport_mode.split("+")) <= {"AIR", "SEA", "未分配"})
            self.assertIn(decision.approval_status, {"待审批", "自动通过"})
            self.assertIn(decision.optimizer, {"scipy.optimize.milp", "fallback"})
            self.assertLessEqual(
                decision.allocated_units,
                need_by_key[(decision.sku, decision.warehouse_id)],
            )

        for sku, allocated in allocated_by_sku.items():
            self.assertLessEqual(allocated, network.global_supply_pool[sku].allocatable_units)
        self.assertTrue(any(decision.optimizer == "scipy.optimize.milp" for decision in allocations))

    def test_simulation_compares_baseline_and_intelligent_strategy(self) -> None:
        network = load_network(ROOT / "data/sample_network.json")
        comparison = compare_strategies(network, days=90)

        self.assertGreaterEqual(comparison.intelligent.fill_rate, comparison.baseline.fill_rate)
        self.assertLessEqual(comparison.intelligent.stockout_rate, comparison.baseline.stockout_rate)
        self.assertIn("profit_lift", comparison.improvement)


if __name__ == "__main__":
    unittest.main()
