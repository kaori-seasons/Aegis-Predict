"""AEGIS-PREDICT production platform.

`overseas_inventory` keeps the forecasting / allocation / simulation kernels and the
competition demo. `aegis` wraps those kernels with a PostgreSQL-backed data platform:
multi-tenant storage with row level security, a connector runtime for ERP / market
data, a governed plan lifecycle and a versioned HTTP API.
"""

__version__ = "0.12.0"
