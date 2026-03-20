"""
edlo_adapter.py — Edlo API → standard SKU catalog format

Edlo is a Brazilian surgical instrument manufacturer.
API docs: (fill in when received from customer)

Output: catalogs/edlo.json — array of {sku, name, manufacturer}

Usage:
    python3 adapters/edlo_adapter.py
    python3 adapters/edlo_adapter.py --dry-run   # print instead of writing
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Configuration — fill in when customer provides API credentials
# Can also be set via environment variables
# ─────────────────────────────────────────────────────────────────────────────

API_BASE_URL  = os.getenv("EDLO_API_URL",   "https://api.edlo.com.br")   # TODO: confirm
API_KEY       = os.getenv("EDLO_API_KEY",   "")                           # TODO: fill in
OUTPUT_FILE   = Path("catalogs/edlo.json")

# ─────────────────────────────────────────────────────────────────────────────
# Field mapping — update these once you see the real API response shape
#
# Example raw item (placeholder — replace with actual field names from docs):
# {
#   "item_code":   "ED-102",
#   "description": "Tesoura Metzenbaum Curva 14cm Inox",
#   "brand":       "Edlo",
#   "category":    "Tesouras",
#   "active":      true,
#   "price_brl":   145.00
# }
# ─────────────────────────────────────────────────────────────────────────────

FIELD_SKU          = "item_code"       # unique identifier
FIELD_NAME         = "description"     # human-readable product name (used for mapping)
FIELD_ACTIVE       = "active"          # set to None if no active/inactive flag
ITEMS_PATH         = "items"           # JSON key that holds the array (None if root is array)


def fetch_catalog() -> list[dict]:
    """Fetch full catalog from Edlo API. Returns raw API response items."""
    try:
        import httpx
    except ImportError:
        print("ERROR: httpx not installed. Run: pip3 install httpx")
        sys.exit(1)

    if not API_KEY:
        print("ERROR: EDLO_API_KEY not set.")
        print("Set it with: export EDLO_API_KEY=your_key_here")
        sys.exit(1)

    headers = {"Authorization": f"Bearer {API_KEY}"}

    # TODO: update endpoint path once confirmed with customer
    # Common patterns: /catalog, /products, /instruments, /items, /v1/products
    url = f"{API_BASE_URL}/catalog"

    print(f"Fetching Edlo catalog from {url}...")
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()

    data = resp.json()

    # Unwrap if items are nested under a key
    if ITEMS_PATH and isinstance(data, dict):
        items = data.get(ITEMS_PATH, [])
    else:
        items = data  # root is already an array

    print(f"  Fetched {len(items)} raw items.")
    return items


def normalize(items: list[dict]) -> list[dict]:
    """Convert raw Edlo API items to standard {sku, name, manufacturer} format."""
    normalized = []
    skipped = 0

    for item in items:
        # Skip inactive items if the field exists
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
            "manufacturer": "Edlo",
        })

    if skipped:
        print(f"  Skipped {skipped} items (inactive or missing fields).")
    print(f"  Normalized {len(normalized)} items.")
    return normalized


def main() -> None:
    parser = argparse.ArgumentParser(description="Edlo API adapter")
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
