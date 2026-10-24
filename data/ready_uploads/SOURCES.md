# Ready-to-upload public dataset

## Files

- `data/ready_uploads/uci_online_retail_globalpool_upload.json`
- Source workbook: `data/public_sources/Online Retail.xlsx`

## Public source

UCI Machine Learning Repository, Online Retail dataset:
https://archive.ics.uci.edu/dataset/352/online+retail

The UCI page describes the file as a transactional dataset for a UK-based non-store online retailer, covering 2010-12-01 to 2011-12-09. It includes `StockCode`, `Description`, `Quantity`, `InvoiceDate`, `UnitPrice`, and `Country`.

## Transformation

- Selected SKU stock codes: 85099B, 85123A, 21212
- Countries mapped to warehouses: United Kingdom -> UK-BHX, Germany -> DE-HAM, France -> FR-CDG, Australia -> AU-SYD.
- `demand_history` is aggregated from public transactions.
- Sparse daily demand is smoothed only to make it usable as operational daily forecast input.
- Inventory, transport, procurement, capital, and market signal fields are simulation fields generated from demand scale.

## Row count used

Filtered positive-quantity rows across selected countries: 503752
