"""
rhosse_adapter.py — Rhosse API → standard SKU catalog format

Rhosse is a Brazilian surgical instrument manufacturer.
API docs: (fill in when received from customer)

Output: catalogs/rhosse.json — array of {sku, name, manufacturer}

Usage:
    python3 adapters/rhosse_adapter.py
    python3 adapters/rhosse_adapter.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

API_BASE_URL  = os.getenv("RHOSSE_API_URL",  "https://api.rhosse.com.br")  # TODO: confirm
API_KEY       = os.getenv("RHOSSE_API_KEY",  "")
OUTPUT_FILE   = Path("catalogs/rhosse.json")

# ─────────────────────────────────────────────────────────────────────────────
# Field mapping — update once you see the real API response
#
# Example raw item (placeholder):
# {
#   "codigo":    "RHO-44",
#   "nome":      "Pinça Cirúrgica Dente de Rato 14cm",
#   "categoria": "Pinças",
#   "ativo":     true
# }
# ─────────────────────────────────────────────────────────────────────────────

FIELD_SKU    = "codigo"     # Portuguese: "código" = code
FIELD_NAME   = "nome"       # Portuguese: "nome" = name
FIELD_ACTIVE = "ativo"      # Portuguese: "ativo" = active
ITEMS_PATH   = "produtos"   # Portuguese: "produtos" = products (None if root is array)


def fetch_catalog() -> list[dict]:
    try:
        import httpx
    except ImportError:
        print("ERROR: httpx not installed. Run: pip3 install httpx")
        sys.exit(1)

    if not API_KEY:
        print("ERROR: RHOSSE_API_KEY not set.")
        print("Set it with: export RHOSSE_API_KEY=your_key_here")
        sys.exit(1)

    headers = {"Authorization": f"Bearer {API_KEY}"}

    # TODO: update endpoint path once confirmed with customer
    url = f"{API_BASE_URL}/produtos"

    print(f"Fetching Rhosse catalog from {url}...")
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()

    data = resp.json()

    if ITEMS_PATH and isinstance(data, dict):
        items = data.get(ITEMS_PATH, [])
    else:
        items = data

    print(f"  Fetched {len(items)} raw items.")
    return items


def normalize(items: list[dict]) -> list[dict]:
    normalized = []
    skipped = 0

    for item in items:
        if FIELD_ACTIVE and not item.get(FIELD_ACTIVE, True):
            skipped += 1
            continue

        sku  = str(item.get(FIELD_SKU, "")).strip()
        name = str(item.get(FIELD_NAME, "")).strip()

        if not sku or not name:
            skipped += 1
            continue

        normalized.append({
            "sku":          sku,
            "name":         name,
            "manufacturer": "Rhosse",
        })

    if skipped:
        print(f"  Skipped {skipped} items (inactive or missing fields).")
    print(f"  Normalized {len(normalized)} items.")
    return normalized


def main() -> None:
    parser = argparse.ArgumentParser(description="Rhosse API adapter")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print normalized output instead of writing to file")
    args = parser.parse_args()

    raw   = fetch_catalog()
    items = normalize(raw)

    if args.dry_run:
        print(json.dumps(items[:5], indent=2, ensure_ascii=False))
        print(f"... ({len(items)} total items)")
        return

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(items, indent=2, ensure_ascii=False))
    print(f"Written to {OUTPUT_FILE}")
    print(f"\nNext step:")
    print(f"  python3 scripts/semantic_map_skus.py --input {OUTPUT_FILE} --merge data/sku_mapping.json --threshold-auto 0.60 --threshold-review 0.40")


if __name__ == "__main__":
    main()
