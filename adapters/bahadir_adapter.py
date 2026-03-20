"""
bahadir_adapter.py — Bahadir API → standard SKU catalog format

Bahadir is a Turkish surgical instrument manufacturer.
API likely returns a mix of English, Turkish, and German field names/values.
API docs: (fill in when received from customer)

Output: catalogs/bahadir.json — array of {sku, name, manufacturer}

Usage:
    python3 adapters/bahadir_adapter.py
    python3 adapters/bahadir_adapter.py --dry-run
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

API_BASE_URL  = os.getenv("BAHADIR_API_URL",  "https://api.bahadirtibbi.com")  # TODO: confirm
API_KEY       = os.getenv("BAHADIR_API_KEY",  "")
OUTPUT_FILE   = Path("catalogs/bahadir.json")

# ─────────────────────────────────────────────────────────────────────────────
# Field mapping — update once you see the real API response
#
# Bahadir may return Turkish or English field names.
# Common Turkish field names: urun_kodu=product_code, urun_adi=product_name,
#                              aktif=active, marka=brand, kategori=category
#
# Example raw item (placeholder):
# {
#   "urun_kodu": "BAH-11",
#   "urun_adi":  "Metzenbaum Makas 18cm Eğri",
#   "kategori":  "Makas",
#   "aktif":     true
# }
# ─────────────────────────────────────────────────────────────────────────────

# Primary field names (Turkish). Fallbacks tried if primary not found.
FIELD_SKU_OPTIONS    = ["urun_kodu", "product_code", "sku", "code", "item_code", "ref"]
FIELD_NAME_OPTIONS   = ["urun_adi",  "product_name", "name", "description", "aciklama", "bezeichnung"]
FIELD_ACTIVE_OPTIONS = ["aktif",     "active",       "is_active", "status"]
ITEMS_PATH_OPTIONS   = ["urunler",   "products",     "items",     "data", None]


def _find_field(item: dict, candidates: list[str]) -> str | None:
    """Try field name candidates in order, return first match."""
    for key in candidates:
        if key in item:
            return item[key]
    return None


def fetch_catalog() -> list[dict]:
    try:
        import httpx
    except ImportError:
        print("ERROR: httpx not installed. Run: pip3 install httpx")
        sys.exit(1)

    if not API_KEY:
        print("ERROR: BAHADIR_API_KEY not set.")
        print("Set it with: export BAHADIR_API_KEY=your_key_here")
        sys.exit(1)

    # Bahadir may use API key as query param or header — try both patterns
    # TODO: confirm auth method with customer
    headers = {"Authorization": f"Bearer {API_KEY}",
               "X-API-Key": API_KEY}

    # TODO: update endpoint path once confirmed
    url = f"{API_BASE_URL}/urunler"  # Turkish: "urunler" = products

    print(f"Fetching Bahadir catalog from {url}...")
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()

    data = resp.json()

    # Try multiple possible wrapper keys
    items = None
    if isinstance(data, list):
        items = data
    else:
        for key in ITEMS_PATH_OPTIONS:
            if key and key in data:
                items = data[key]
                break
        if items is None:
            items = data  # fallback

    print(f"  Fetched {len(items)} raw items.")
    return items


def normalize(items: list[dict]) -> list[dict]:
    normalized = []
    skipped = 0

    for item in items:
        # Check active status using any matching field name
        active_val = _find_field(item, FIELD_ACTIVE_OPTIONS)
        if active_val is not None and not active_val:
            skipped += 1
            continue

        sku  = str(_find_field(item, FIELD_SKU_OPTIONS)  or "").strip()
        name = str(_find_field(item, FIELD_NAME_OPTIONS) or "").strip()

        if not sku or not name:
            skipped += 1
            continue

        normalized.append({
            "sku":          sku,
            "name":         name,
            "manufacturer": "Bahadir",
        })

    if skipped:
        print(f"  Skipped {skipped} items (inactive or missing fields).")
    print(f"  Normalized {len(normalized)} items.")
    return normalized


def main() -> None:
    parser = argparse.ArgumentParser(description="Bahadir API adapter")
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
