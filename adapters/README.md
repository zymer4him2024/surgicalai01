# Manufacturer API Adapters

Each adapter pulls a manufacturer's catalog and normalizes it to the standard format for `semantic_map_skus.py`.

## Standard output format

```json
[
  {"sku": "ED-102", "name": "Tesoura Metzenbaum Curva 14cm Inox", "manufacturer": "Edlo"}
]
```

## Workflow

```
Manufacturer API  →  adapter  →  catalogs/*.json  →  semantic_map_skus.py  →  data/sku_mapping.json
```

### 1. Set credentials

```bash
export EDLO_API_KEY=your_key
export RHOSSE_API_KEY=your_key
export BAHADIR_API_KEY=your_key
```

### 2. Pull catalogs

```bash
python3 adapters/edlo_adapter.py
python3 adapters/rhosse_adapter.py
python3 adapters/bahadir_adapter.py
```

### 3. Map to SurgeoNet classes (merges all into one file)

```bash
python3 scripts/semantic_map_skus.py --input catalogs/edlo.json   --merge data/sku_mapping.json --threshold-auto 0.60 --threshold-review 0.40
python3 scripts/semantic_map_skus.py --input catalogs/rhosse.json --merge data/sku_mapping.json --threshold-auto 0.60 --threshold-review 0.40
python3 scripts/semantic_map_skus.py --input catalogs/bahadir.json --merge data/sku_mapping.json --threshold-auto 0.60 --threshold-review 0.40
```

## Adapter setup checklist (per manufacturer)

When you receive API documentation from a new customer:

1. Update `API_BASE_URL` with the real base URL
2. Update the endpoint path inside `fetch_catalog()` (e.g. `/catalog`, `/products`, `/v1/items`)
3. Update `FIELD_SKU`, `FIELD_NAME`, `FIELD_ACTIVE` to match their actual field names
4. Update `ITEMS_PATH` to the key that wraps the array (or `None` if root is array)
5. Check auth method: Bearer token, API key header, or query param
6. Run `--dry-run` first to verify the normalization before writing

## Adding a new manufacturer

Copy any existing adapter, rename it, and update the 5 fields above.
The `normalize()` function is identical across all adapters — only the config changes.
