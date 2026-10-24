# Public sources

This folder is intentionally left light in git.

The project can run with the built-in sample network:

```text
data/sample_network.json
```

If you want the original UCI Online Retail files used by the conversion script:

- Dataset page: https://archive.ics.uci.edu/dataset/352/online+retail
- Place the workbook here as `Online Retail.xlsx`
- Optional zip: `uci_online_retail.zip`

Then rebuild a ready upload JSON with:

```bash
python scripts/build_uci_ready_upload.py
```

Generated ready-to-use JSON already lives in `data/ready_uploads/`.
