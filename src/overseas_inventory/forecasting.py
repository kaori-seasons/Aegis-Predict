from __future__ import annotations

from dataclasses import dataclass
from math import cos, log10, pi, sin
import os
import random
from statistics import fmean, pstdev
from typing import Protocol

from .models import DemandSeries, MarketSignal


MIN_HISTORY_DAYS = 14
TARGET_WINDOW_DAYS = 7
_IMPORT_CACHE: dict[str, object | str] = {}


@dataclass(frozen=True)
class ForecastResult:
    sku: str
    warehouse_id: str
    model_name: str
    daily_rate: float
    horizon_days: int
    horizon_units: int
    p50_units: int
    p80_units: int
    p95_units: int
    volatility: float
    confidence: float
    signal_lift: float
    feature_notes: str


class DemandRegressor(Protocol):
    name: str

    def fit(self, features: list[list[float]], targets: list[float]) -> None:
        ...

    def predict_one(self, features: list[float]) -> float:
        ...


@dataclass
class _TreeNode:
    value: float
    feature_index: int | None = None
    threshold: float | None = None
    left: "_TreeNode | None" = None
    right: "_TreeNode | None" = None


class PurePythonRandomForestRegressor:
    """Small deterministic random-forest regressor used when ML wheels are unavailable."""

    name = "RandomForestRegressor(pure-python)"

    def __init__(
        self,
        n_estimators: int = 9,
        max_depth: int = 4,
        min_leaf: int = 6,
        max_training_rows: int = 900,
        random_state: int = 42,
    ) -> None:
        self.n_estimators = n_estimators
        self.max_depth = max_depth
        self.min_leaf = min_leaf
        self.max_training_rows = int(os.environ.get("GLOBALPOOL_ML_MAX_ROWS", max_training_rows))
        self.random_state = random_state
        self.trees: list[_TreeNode] = []
        self.global_mean = 0.0
        self.feature_count = 0

    def fit(self, features: list[list[float]], targets: list[float]) -> None:
        if not features or not targets:
            self.global_mean = 0.0
            self.trees = []
            return

        rng = random.Random(self.random_state)
        rows = list(range(len(features)))
        if len(rows) > self.max_training_rows:
            rows = rng.sample(rows, self.max_training_rows)

        self.global_mean = fmean(targets[index] for index in rows)
        self.feature_count = len(features[0])
        self.trees = []
        for tree_index in range(self.n_estimators):
            tree_rng = random.Random(self.random_state + tree_index * 17)
            sample_size = max(20, int(len(rows) * 0.82))
            sample = [tree_rng.choice(rows) for _ in range(sample_size)]
            self.trees.append(self._build_tree(features, targets, sample, 0, tree_rng))

    def predict_one(self, features: list[float]) -> float:
        if not self.trees:
            return self.global_mean
        return max(0.0, fmean(self._predict_tree(tree, features) for tree in self.trees))

    def _build_tree(
        self,
        features: list[list[float]],
        targets: list[float],
        rows: list[int],
        depth: int,
        rng: random.Random,
    ) -> _TreeNode:
        value = fmean(targets[index] for index in rows)
        if depth >= self.max_depth or len(rows) < self.min_leaf * 2 or self._target_variance(targets, rows) < 0.04:
            return _TreeNode(value=value)

        split = self._best_random_split(features, targets, rows, rng)
        if split is None:
            return _TreeNode(value=value)

        feature_index, threshold, left_rows, right_rows = split
        return _TreeNode(
            value=value,
            feature_index=feature_index,
            threshold=threshold,
            left=self._build_tree(features, targets, left_rows, depth + 1, rng),
            right=self._build_tree(features, targets, right_rows, depth + 1, rng),
        )

    def _best_random_split(
        self,
        features: list[list[float]],
        targets: list[float],
        rows: list[int],
        rng: random.Random,
    ) -> tuple[int, float, list[int], list[int]] | None:
        feature_count = max(1, self.feature_count)
        candidate_count = min(feature_count, max(4, int(feature_count ** 0.5) + 2))
        candidate_features = rng.sample(range(feature_count), candidate_count)
        best: tuple[float, int, float, list[int], list[int]] | None = None

        for feature_index in candidate_features:
            values = [features[index][feature_index] for index in rows]
            low, high = min(values), max(values)
            if low == high:
                continue
            thresholds = [rng.uniform(low, high) for _ in range(3)]
            for threshold in thresholds:
                left = [index for index in rows if features[index][feature_index] <= threshold]
                right_count = len(rows) - len(left)
                if len(left) < self.min_leaf or right_count < self.min_leaf:
                    continue
                left_set = set(left)
                right = [index for index in rows if index not in left_set]
                score = self._sse(targets, left) + self._sse(targets, right)
                if best is None or score < best[0]:
                    best = (score, feature_index, threshold, left, right)

        if best is None:
            return None
        _, feature_index, threshold, left, right = best
        return feature_index, threshold, left, right

    @staticmethod
    def _target_variance(targets: list[float], rows: list[int]) -> float:
        if len(rows) < 2:
            return 0.0
        mean = fmean(targets[index] for index in rows)
        return fmean((targets[index] - mean) ** 2 for index in rows)

    @staticmethod
    def _sse(targets: list[float], rows: list[int]) -> float:
        if not rows:
            return 0.0
        mean = fmean(targets[index] for index in rows)
        return sum((targets[index] - mean) ** 2 for index in rows)

    @staticmethod
    def _predict_tree(tree: _TreeNode, features: list[float]) -> float:
        node = tree
        while node.feature_index is not None and node.threshold is not None:
            if features[node.feature_index] <= node.threshold:
                if node.left is None:
                    break
                node = node.left
            else:
                if node.right is None:
                    break
                node = node.right
        return node.value


class ExternalRegressor:
    def __init__(self, name: str, model: object) -> None:
        self.name = name
        self.model = model

    def fit(self, features: list[list[float]], targets: list[float]) -> None:
        fit = getattr(self.model, "fit")
        fit(features, targets)

    def predict_one(self, features: list[float]) -> float:
        predict = getattr(self.model, "predict")
        prediction = predict([features])
        return float(prediction[0])


class MLForecastEngine:
    def __init__(
        self,
        series: list[DemandSeries],
        horizon_days: int,
        signals: dict[tuple[str, str], MarketSignal] | None = None,
    ) -> None:
        self.series = series
        self.horizon_days = horizon_days
        self.signals = signals or {}
        self.sku_codes = _normalized_codes([item.sku for item in series])
        self.warehouse_codes = _normalized_codes([item.warehouse_id for item in series])
        self.training_sample_count = 0
        self.active_models: list[DemandRegressor] = []
        self.unavailable_models: list[str] = []
        features, targets = self._build_training_set()
        self.training_sample_count = len(targets)
        self._fit_models(features, targets)

    def forecast(self, series: DemandSeries, signal: MarketSignal | None = None) -> ForecastResult:
        values = series.daily_units
        if not values:
            raise ValueError("demand series cannot be empty")

        signal = signal if signal is not None else self.signals.get((series.sku, series.warehouse_id))
        baseline_rate = _sequence_baseline_rate(values, signal, self.horizon_days)
        if self.active_models and self.training_sample_count >= 24 and len(values) >= MIN_HISTORY_DAYS:
            features = self._features_for(series, len(values), signal)
            model_rate = fmean(model.predict_one(features) for model in self.active_models)
            adjusted_daily_rate = max(0.0, model_rate * 0.72 + baseline_rate * 0.28)
        else:
            adjusted_daily_rate = baseline_rate

        recent_values = values[-30:] if len(values) >= 30 else values
        volatility = pstdev(recent_values) if len(recent_values) > 1 else max(1.0, adjusted_daily_rate * 0.25)
        coefficient_of_variation = volatility / max(adjusted_daily_rate, 1.0)
        confidence = _confidence_score(
            coefficient_of_variation=coefficient_of_variation,
            signal=signal,
            model_count=len(self.active_models),
            training_sample_count=self.training_sample_count,
        )
        horizon_mean = adjusted_daily_rate * self.horizon_days
        horizon_sigma = volatility * (self.horizon_days ** 0.5)
        signal_lift = _signal_lift(signal)

        return ForecastResult(
            sku=series.sku,
            warehouse_id=series.warehouse_id,
            model_name=self.model_name,
            daily_rate=round(adjusted_daily_rate, 2),
            horizon_days=self.horizon_days,
            horizon_units=round(horizon_mean),
            p50_units=round(horizon_mean),
            p80_units=round(horizon_mean + 0.84 * horizon_sigma),
            p95_units=round(horizon_mean + 1.65 * horizon_sigma),
            volatility=round(volatility, 2),
            confidence=confidence,
            signal_lift=round(signal_lift, 3),
            feature_notes=self.feature_notes,
        )

    @property
    def model_name(self) -> str:
        if not self.active_models:
            return "rolling-stat fallback ensemble"
        names = "/".join(model.name.split("(")[0] for model in self.active_models)
        return f"LightGBM/XGBoost/RandomForest ML ensemble ({names})"

    @property
    def feature_notes(self) -> str:
        active = ", ".join(model.name for model in self.active_models) or "rolling-stat fallback"
        skipped = "; ".join(self.unavailable_models[:3])
        if skipped:
            skipped = f"; skipped={skipped}"
        return (
            "features=lag, rolling mean, volatility, trend, weekday, SKU, warehouse, "
            f"promotion, ad, seasonality, platform event, price; trained_samples={self.training_sample_count}; "
            f"active_models={active}{skipped}"
        )

    def _build_training_set(self) -> tuple[list[list[float]], list[float]]:
        features: list[list[float]] = []
        targets: list[float] = []
        for item in self.series:
            values = item.daily_units
            if len(values) <= MIN_HISTORY_DAYS:
                continue
            signal = self.signals.get((item.sku, item.warehouse_id))
            for index in range(MIN_HISTORY_DAYS, len(values)):
                future = values[index : index + TARGET_WINDOW_DAYS]
                if not future:
                    continue
                features.append(self._features_for(item, index, signal))
                targets.append(float(fmean(future)))
        return features, targets

    def _fit_models(self, features: list[list[float]], targets: list[float]) -> None:
        if len(targets) < 24:
            return

        for factory in (_lightgbm_factory, _xgboost_factory, _sklearn_random_forest_factory):
            name, model_or_error = factory()
            if isinstance(model_or_error, str):
                self.unavailable_models.append(f"{name}: {model_or_error}")
                continue
            try:
                model_or_error.fit(features, targets)
            except Exception as exc:  # noqa: BLE001 - optional ML libraries fail differently by platform.
                self.unavailable_models.append(f"{name}: {exc.__class__.__name__}")
                continue
            self.active_models.append(model_or_error)

        if not any(model.name.startswith("RandomForestRegressor") for model in self.active_models):
            fallback = PurePythonRandomForestRegressor()
            fallback.fit(features, targets)
            self.active_models.append(fallback)

    def _features_for(
        self,
        series: DemandSeries,
        index: int,
        signal: MarketSignal | None,
    ) -> list[float]:
        values = series.daily_units[:index]
        recent_3 = values[-3:]
        recent_7 = values[-7:]
        recent_14 = values[-14:]
        recent_30 = values[-30:] if len(values) >= 30 else values
        lag_1 = values[-1]
        lag_2 = values[-2] if len(values) >= 2 else lag_1
        lag_3 = values[-3] if len(values) >= 3 else lag_1
        lag_7 = values[-7] if len(values) >= 7 else lag_1
        lag_14 = values[-14] if len(values) >= 14 else lag_7
        avg_7 = _mean(recent_7)
        avg_14 = _mean(recent_14)
        avg_30 = _mean(recent_30)
        trend_7_30 = (avg_7 - avg_30) / max(avg_30, 1.0)
        weekday = index % 7
        signal_values = _signal_features(signal)

        return [
            lag_1,
            lag_2,
            lag_3,
            lag_7,
            lag_14,
            _mean(recent_3),
            avg_7,
            avg_14,
            avg_30,
            _std(recent_7),
            _std(recent_30),
            trend_7_30,
            index / 180,
            sin(2 * pi * weekday / 7),
            cos(2 * pi * weekday / 7),
            self.sku_codes.get(series.sku, 0.0),
            self.warehouse_codes.get(series.warehouse_id, 0.0),
            *signal_values,
        ]


def forecast_demand(
    series: DemandSeries,
    horizon_days: int = 30,
    signal: MarketSignal | None = None,
) -> ForecastResult:
    """Forecast one SKU-warehouse demand series with the same ML pipeline used globally."""
    signals = {(series.sku, series.warehouse_id): signal} if signal else {}
    engine = MLForecastEngine([series], horizon_days=horizon_days, signals=signals)
    return engine.forecast(series, signal=signal)


def forecast_all(
    series: list[DemandSeries],
    horizon_days: int = 30,
    signals: dict[tuple[str, str], MarketSignal] | None = None,
) -> list[ForecastResult]:
    engine = MLForecastEngine(series, horizon_days=horizon_days, signals=signals)
    return [
        engine.forecast(
            item,
            signal=(signals or {}).get((item.sku, item.warehouse_id)),
        )
        for item in series
    ]


def _lightgbm_factory() -> tuple[str, DemandRegressor | str]:
    cached = _cached_import("lightgbm", "LGBMRegressor")
    if isinstance(cached, str):
        return "LightGBM", cached
    LGBMRegressor = cached
    model = LGBMRegressor(
        n_estimators=90,
        learning_rate=0.05,
        max_depth=5,
        subsample=0.9,
        colsample_bytree=0.9,
        random_state=42,
        verbosity=-1,
    )
    return "LightGBM", ExternalRegressor("LightGBMRegressor", model)


def _xgboost_factory() -> tuple[str, DemandRegressor | str]:
    cached = _cached_import("xgboost", "XGBRegressor")
    if isinstance(cached, str):
        return "XGBoost", cached
    XGBRegressor = cached
    model = XGBRegressor(
        n_estimators=110,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.9,
        colsample_bytree=0.9,
        objective="reg:squarederror",
        n_jobs=1,
        random_state=42,
        verbosity=0,
    )
    return "XGBoost", ExternalRegressor("XGBRegressor", model)


def _sklearn_random_forest_factory() -> tuple[str, DemandRegressor | str]:
    cached = _cached_import("sklearn.ensemble", "RandomForestRegressor")
    if isinstance(cached, str):
        return "RandomForest", cached
    RandomForestRegressor = cached
    model = RandomForestRegressor(
        n_estimators=90,
        max_depth=8,
        min_samples_leaf=4,
        max_features="sqrt",
        n_jobs=1,
        random_state=42,
    )
    return "RandomForest", ExternalRegressor("RandomForestRegressor", model)


def _cached_import(module_name: str, attribute_name: str) -> object | str:
    cache_key = f"{module_name}.{attribute_name}"
    if cache_key in _IMPORT_CACHE:
        return _IMPORT_CACHE[cache_key]
    try:
        module = __import__(module_name, fromlist=[attribute_name])
        attribute = getattr(module, attribute_name)
    except Exception as exc:  # noqa: BLE001
        _IMPORT_CACHE[cache_key] = exc.__class__.__name__
        return _IMPORT_CACHE[cache_key]
    _IMPORT_CACHE[cache_key] = attribute
    return attribute


def _sequence_baseline_rate(
    values: list[int],
    signal: MarketSignal | None,
    horizon_days: int,
) -> float:
    recent_7 = _mean(values[-7:])
    recent_14 = _mean(values[-14:])
    recent_30 = _mean(values[-30:] if len(values) >= 30 else values)
    short_trend = (recent_7 - recent_30) / max(recent_30, 1.0)
    trend_multiplier = max(0.72, min(1.42, 1 + short_trend * min(horizon_days, 30) / 45))
    base_rate = recent_7 * 0.56 + recent_14 * 0.28 + recent_30 * 0.16
    return max(0.0, base_rate * trend_multiplier * (1 + _signal_lift(signal)))


def _signal_lift(signal: MarketSignal | None) -> float:
    if signal is None:
        return 0.0
    return (
        (signal.ad_spend_index - 1) * 0.18
        + (signal.promotion_lift - 1) * 0.34
        + (signal.seasonal_index - 1) * 0.22
        + (signal.platform_event_lift - 1) * 0.18
        - (signal.price_index - 1) * 0.12
    )


def _signal_features(signal: MarketSignal | None) -> list[float]:
    if signal is None:
        return [0.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    return [
        1.0,
        signal.ad_spend_index,
        signal.promotion_lift,
        signal.seasonal_index,
        signal.platform_event_lift,
        signal.price_index,
    ]


def _confidence_score(
    coefficient_of_variation: float,
    signal: MarketSignal | None,
    model_count: int,
    training_sample_count: int,
) -> float:
    signal_quality = 0.05 if signal else -0.05
    model_bonus = min(0.12, model_count * 0.04)
    sample_bonus = min(0.08, log10(max(10, training_sample_count)) / 30)
    confidence = 1 / (1 + coefficient_of_variation) + signal_quality + model_bonus + sample_bonus
    return round(max(0.35, min(0.96, confidence)), 2)


def _normalized_codes(values: list[str]) -> dict[str, float]:
    unique_values = sorted(set(values))
    if len(unique_values) <= 1:
        return {value: 0.0 for value in unique_values}
    denominator = len(unique_values) - 1
    return {value: index / denominator for index, value in enumerate(unique_values)}


def _mean(values: list[int] | list[float]) -> float:
    return fmean(values) if values else 0.0


def _std(values: list[int] | list[float]) -> float:
    return pstdev(values) if len(values) > 1 else 0.0
