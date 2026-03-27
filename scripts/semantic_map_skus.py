"""
semantic_map_skus.py — Automated SKU → SurgeoNet class mapper

Uses multilingual sentence embeddings (paraphrase-multilingual-mpnet-base-v2) to map
external manufacturer SKU names to the internal 14-class SurgeoNet labels.

Handles: Portuguese (Rhosse, Edlo), Turkish/German (Bahadir), English

Thresholds:
  > AUTO_THRESHOLD  → auto-map (written to sku_mapping.json)
  > REVIEW_THRESHOLD → queued for human review
  < REVIEW_THRESHOLD → flagged as unmapped (AI doesn't recognize this tool)

Usage:
  # Demo with built-in Rhosse/Edlo/Bahadir samples
  python3 scripts/semantic_map_skus.py --demo

  # Map a catalog file
  python3 scripts/semantic_map_skus.py --input catalogs/rhosse.json --output data/sku_mapping.json

  # Merge into existing mapping (non-destructive)
  python3 scripts/semantic_map_skus.py --input catalogs/edlo.json --merge data/sku_mapping.json

Input JSON format:
  [
    {"sku": "ED-102", "name": "Tesoura Metzenbaum Curva 14cm", "manufacturer": "Edlo"},
    ...
  ]

Output JSON format:
  {
    "auto_mapped": {
      "ED-102": {"class": "Metz. Scissor", "score": 0.94, "name": "Tesoura Metzenbaum Curva 14cm", "manufacturer": "Edlo"}
    },
    "review_queue": [
      {"sku": "RHO-77", "name": "...", "best_class": "Sur. Forceps", "score": 0.73, ...}
    ],
    "unmapped": [
      {"sku": "BAH-55", "name": "...", "best_class": "Hook", "score": 0.51, ...}
    ]
  }
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

# ─────────────────────────────────────────────────────────────────────────────
# Thresholds
# ─────────────────────────────────────────────────────────────────────────────

AUTO_THRESHOLD   = 0.82   # auto-write to mapping
REVIEW_THRESHOLD = 0.65   # flag for human review
# < REVIEW_THRESHOLD → unmapped (AI doesn't recognize this class)


# ─────────────────────────────────────────────────────────────────────────────
# Anchor descriptions — rich, multilingual, multi-sentence per class
#
# Strategy: each anchor contains English clinical terms + common abbreviations
# + Portuguese equivalents (Rhosse/Edlo) + Turkish/German equivalents (Bahadir).
# Sentence transformers embed meaning, not keywords, so "Tesoura" and "Scissor"
# naturally cluster together in the embedding space.
# ─────────────────────────────────────────────────────────────────────────────

ANCHORS: dict[str, str] = {
    "Metz. Scissor": (
        "Metzenbaum scissors. Dissecting scissors with long slender blades. "
        "Used for cutting delicate tissue. Curved or straight. "
        "Tesoura Metzenbaum. Tesoura dissecção curva. "
        "Metzschere. Präparierschere Metzenbaum."
    ),
    "Sur. Scissor": (
        "Surgical scissors. Mayo scissors. Operating scissors. "
        "General purpose surgical cutting instrument. Straight or curved blades. "
        "Tesoura Mayo. Tesoura cirúrgica reta. Tesoura de ponta romba. "
        "Mayo-Schere. Chirurgische Schere."
    ),
    "Needle Holder": (
        "Needle holder. Needle driver. Instrument to grip and pass suture needles. "
        "Porta-agulha. Porta agulha cirúrgico Mayo Hegar. "
        "Nadelhalter. Nadelträger chirurgisch."
    ),
    "Sur. Forceps": (
        "Surgical tissue forceps. Dressing forceps. Thumb forceps with teeth. "
        "General grasping instrument for tissue manipulation. "
        "Pinça cirúrgica. Pinça de tecido com dente. Pinça anatômica. "
        "Chirurgische Pinzette. Gewebepinzette."
    ),
    "Atr. Forceps": (
        "Atraumatic forceps. DeBakey forceps. Non-crushing vascular forceps. "
        "Delicate tissue grasper without teeth to prevent injury. "
        "Pinça atraumática. Pinça DeBakey. Pinça vascular. "
        "Atraumatische Pinzette. DeBakey Pinzette. Gefäßpinzette."
    ),
    "Scalpel": (
        "Scalpel. Surgical knife. Blade and handle for incision. "
        "Bisturi. Cabo de bisturi. Lâmina cirúrgica. "
        "Skalpell. Operationsmesser. Bisturiklinge."
    ),
    "Retractor": (
        "Surgical retractor. Self-retaining retractor. Wound retractor. "
        "Instrument to hold back tissue and expose surgical field. "
        "Afastador cirúrgico. Afastador autoestático. Afastador de tecidos. "
        "Wundhaken. Wundspreizer. Selbsthaltender Retraktor."
    ),
    "Lig. Clamp": (
        "Ligature clamp. Kelly clamp. Hemostatic forceps. Mosquito clamp. "
        "Used to clamp blood vessels for ligation. Curved or straight. "
        "Pinça Kelly. Pinça hemostática. Pinça mosquito. Pinça de ligadura. "
        "Klemme Kelly. Hämostatische Klemme. Mosquito-Klemme."
    ),
    "Peri. Clamp": (
        "Pean clamp. Kocher clamp. Straight hemostatic clamp. "
        "Heavy crushing clamp for larger vessels and tissue pedicles. "
        "Pinça Pean. Pinça de Kocher. Pinça peritoneal. Pinça reta. "
        "Péan-Klemme. Kocher-Klemme. Gerade Gefäßklemme."
    ),
    "Overholt Clamp": (
        "Overholt clamp. Right angle clamp. Mixter clamp. "
        "Curved dissecting and ligating clamp for passing suture around vessels. "
        "Pinça Overholt. Pinça angulada. Pinça em ângulo reto. "
        "Overholt-Klemme. Rechtwinklige Klemme. Dissektionsklemme."
    ),
    "Hook": (
        "Surgical hook. Skin hook. Single sharp hook for tissue retraction. "
        "Gancho cirúrgico. Gancho de pele. Gancho de retração. "
        "Wundhaken. Hauthaken. Chirurgischer Haken."
    ),
    "Bowl": (
        "Surgical bowl. Instrument bowl. Basin for holding fluids or instruments. "
        "Kidney dish. Emesis basin. "
        "Cubeta cirúrgica. Cuba rim. Recipiente cirúrgico. Bacia. "
        "Nierenschale. Instrumentenschale. OP-Schüssel."
    ),
    "Tong": (
        "Surgical tong. Sponge forceps. Ring forceps. Foerster clamp. "
        "Instrument to hold gauze, sponges, or specimens. Long ring-handled. "
        "Pinça de Foerster. Pinça anel. Pinça de esponja. Pinça porta-esponja. "
        "Ringklemme. Kornzange. Spongienzange. Tupferzange."
    ),
}

SURGEONET_CLASSES = list(ANCHORS.keys())


# ─────────────────────────────────────────────────────────────────────────────
# Demo catalog — realistic Rhosse/Edlo/Bahadir samples for testing
# ─────────────────────────────────────────────────────────────────────────────

DEMO_CATALOG: list[dict[str, str]] = [
    # Edlo (Brazilian, Portuguese)
    {"sku": "ED-102", "name": "Tesoura Metzenbaum Curva 14cm Inox", "manufacturer": "Edlo"},
    {"sku": "ED-115", "name": "Tesoura Mayo Reta 17cm", "manufacturer": "Edlo"},
    {"sku": "ED-201", "name": "Porta Agulha Mayo-Hegar 18cm", "manufacturer": "Edlo"},
    {"sku": "ED-310", "name": "Pinça DeBakey 20cm Atraumática Cardiovascular", "manufacturer": "Edlo"},
    {"sku": "ED-408", "name": "Pinça Kelly Curva Hemostática 14cm", "manufacturer": "Edlo"},
    {"sku": "ED-412", "name": "Pinça Pean Reta 16cm", "manufacturer": "Edlo"},
    {"sku": "ED-501", "name": "Afastador Farabeuf Auto-Estático", "manufacturer": "Edlo"},
    {"sku": "ED-602", "name": "Bisturi Cabo Nr 4 Inox Cirúrgico", "manufacturer": "Edlo"},
    {"sku": "ED-710", "name": "Pinça Foerster Anel Esponja 24cm", "manufacturer": "Edlo"},
    {"sku": "ED-803", "name": "Pinça Overholt Angulada 20cm", "manufacturer": "Edlo"},
    # Rhosse (Brazilian, Portuguese)
    {"sku": "RHO-44",  "name": "Pinça Cirúrgica Dente de Rato 14cm", "manufacturer": "Rhosse"},
    {"sku": "RHO-55",  "name": "Tesoura Dissecção Metzenbaum 18cm Curva", "manufacturer": "Rhosse"},
    {"sku": "RHO-67",  "name": "Pinça Kocher Reta 16cm Dentada", "manufacturer": "Rhosse"},
    {"sku": "RHO-88",  "name": "Porta-Agulha Mathieu 15cm", "manufacturer": "Rhosse"},
    {"sku": "RHO-99",  "name": "Cuba Rim Inox 26cm", "manufacturer": "Rhosse"},
    {"sku": "RHO-120", "name": "Gancho de Pele Simples Agudo", "manufacturer": "Rhosse"},
    {"sku": "RHO-133", "name": "Pinça Mosquito Curva 12.5cm", "manufacturer": "Rhosse"},
    # Bahadir (Turkish, mix of EN/TR/DE)
    {"sku": "BAH-11",  "name": "Metzenbaum Makas 18cm Eğri", "manufacturer": "Bahadir"},   # Makas=scissors, Eğri=curved
    {"sku": "BAH-22",  "name": "Nadelhalter Mathieu 16cm", "manufacturer": "Bahadir"},
    {"sku": "BAH-33",  "name": "Kelly Klemp Düz Hemostatik", "manufacturer": "Bahadir"},    # Klemp=clamp, Düz=straight
    {"sku": "BAH-44",  "name": "DeBakey Damar Pensi 20cm", "manufacturer": "Bahadir"},     # Pensi=forceps, Damar=vessel
    {"sku": "BAH-55",  "name": "Overholt Diseksyon Klemp", "manufacturer": "Bahadir"},
    {"sku": "BAH-66",  "name": "Skalpell Griff Nr 3 Paslanmaz", "manufacturer": "Bahadir"}, # Griff=handle, Paslanmaz=stainless
    {"sku": "BAH-77",  "name": "Kornzange Gerade 24cm", "manufacturer": "Bahadir"},         # Kornzange=ring/sponge forceps
    {"sku": "BAH-88",  "name": "Nierenschale 26cm Paslanmaz Celik", "manufacturer": "Bahadir"}, # Celik=steel
    {"sku": "BAH-99",  "name": "Wundhaken Einzinkig Scharf", "manufacturer": "Bahadir"},   # single-tine sharp hook
    # Edge cases — unknown or ambiguous tools to test unmapped detection
    {"sku": "ED-999",  "name": "Irrigador Laparoscópico 5mm", "manufacturer": "Edlo"},     # laparoscopic irrigator — not in 14 classes
    {"sku": "RHO-999", "name": "Cânula de Aspiração Cirúrgica", "manufacturer": "Rhosse"},  # suction cannula — not in 14 classes
    {"sku": "BAH-999", "name": "Laparoskopik Trokar 12mm", "manufacturer": "Bahadir"},     # trocar — not in 14 classes
]


# ─────────────────────────────────────────────────────────────────────────────
# Core mapper
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Stage 1: Translation
# Translates non-English SKU names to English before embedding.
# This is the key fix: paraphrase models cluster all surgical instruments
# together in embedding space, making cosine similarity unreliable for
# within-domain discrimination. Translating to English first, then comparing
# English-to-English, gives precise separation between instrument types.
# ─────────────────────────────────────────────────────────────────────────────

def translate_to_english(names: list[str], delay: float = 0.05) -> list[str]:
    """Translate a list of SKU names to English using Google Translate (free)."""
    try:
        from deep_translator import GoogleTranslator
    except ImportError:
        print("WARNING: deep-translator not installed. Skipping translation.")
        print("Run: pip3 install deep-translator")
        print("Falling back to original names (accuracy will be lower for PT/TR/DE)\n")
        return names

    translator = GoogleTranslator(source="auto", target="en")
    translated = []
    print(f"Translating {len(names)} SKU names to English...")
    for i, name in enumerate(names):
        try:
            result = translator.translate(name)
            translated.append(result or name)
            if delay:
                time.sleep(delay)   # avoid rate limiting
        except Exception as exc:
            print(f"  Translation failed for '{name}': {exc} — using original")
            translated.append(name)
        if (i + 1) % 10 == 0:
            print(f"  {i + 1}/{len(names)} translated...")
    return translated


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2: English embedding + cosine similarity
# ─────────────────────────────────────────────────────────────────────────────

def load_model():
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        print("ERROR: sentence-transformers not installed.")
        print("Run: pip3 install sentence-transformers")
        sys.exit(1)

    print("Loading English embedding model (first run downloads ~420MB)...")
    # all-mpnet-base-v2: best English-language semantic similarity model.
    # After translation, English-to-English similarity is precise and reliable.
    model = SentenceTransformer("all-mpnet-base-v2")

    # Sanity check: must score high for same-concept, low for different
    from sentence_transformers import util
    pairs = [
        ("Metzenbaum dissecting scissors curved", "Metzenbaum Scissors Curved 14cm", True),
        ("needle holder suture driver",            "Needle Holder Mayo Hegar 18cm",   True),
        ("scalpel surgical knife blade",           "Kidney bowl basin dish",           False),
    ]
    print("Sanity checks:")
    for a, b, should_be_high in pairs:
        e1 = model.encode([a], convert_to_tensor=True)
        e2 = model.encode([b], convert_to_tensor=True)
        score = float(util.cos_sim(e1, e2)[0][0])
        status = "OK" if (score > 0.7) == should_be_high else "WARN"
        print(f"  [{status}] {score:.3f}  '{a[:35]}' ↔ '{b[:35]}'")
    return model


def build_anchor_embeddings(model) -> dict[str, Any]:
    """Pre-compute anchor embeddings for all 14 SurgeoNet classes."""
    texts = list(ANCHORS.values())
    labels = list(ANCHORS.keys())
    print(f"Embedding {len(labels)} SurgeoNet class anchors...")
    vecs = model.encode(texts, convert_to_tensor=True, show_progress_bar=False)
    return {"labels": labels, "matrix": vecs}


def map_catalog(
    catalog: list[dict[str, str]],
    model,
    anchor_embeddings: dict[str, Any],
    auto_threshold: float = AUTO_THRESHOLD,
    review_threshold: float = REVIEW_THRESHOLD,
    skip_translation: bool = False,
) -> dict[str, Any]:
    """Translate SKU names to English, embed, and match against SurgeoNet classes."""
    from sentence_transformers import util

    original_names = [entry["name"] for entry in catalog]

    if skip_translation:
        english_names = original_names
    else:
        english_names = translate_to_english(original_names)

    print(f"\nEmbedding {len(english_names)} translated SKU names...")
    sku_vecs = model.encode(english_names, convert_to_tensor=True, show_progress_bar=True)

    anchor_labels = anchor_embeddings["labels"]
    anchor_matrix = anchor_embeddings["matrix"]

    # sim_matrix: (num_skus, num_classes)
    sim_matrix = util.cos_sim(sku_vecs, anchor_matrix)

    auto_mapped: dict[str, dict] = {}
    review_queue: list[dict] = []
    unmapped: list[dict] = []

    for i, entry in enumerate(catalog):
        scores = sim_matrix[i]
        best_idx = int(scores.argmax())
        best_score = float(scores[best_idx])
        best_class = anchor_labels[best_idx]
        translated = english_names[i]

        result = {
            "sku":          entry["sku"],
            "name":         entry["name"],
            "translated":   translated if translated != entry["name"] else None,
            "manufacturer": entry.get("manufacturer", ""),
            "best_class":   best_class,
            "score":        round(best_score, 4),
        }

        if best_score >= auto_threshold:
            auto_mapped[entry["sku"]] = {
                "class":        best_class,
                "score":        round(best_score, 4),
                "name":         entry["name"],
                "translated":   translated if translated != entry["name"] else None,
                "manufacturer": entry.get("manufacturer", ""),
            }
        elif best_score >= review_threshold:
            review_queue.append(result)
        else:
            unmapped.append(result)

    return {
        "auto_mapped":    auto_mapped,
        "review_queue":   review_queue,
        "unmapped":       unmapped,
        "_thresholds":    {"auto": auto_threshold, "review": review_threshold},
    }


def print_report(results: dict[str, Any]) -> None:
    auto     = results["auto_mapped"]
    review   = results["review_queue"]
    unmapped = results["unmapped"]
    thresh   = results.get("_thresholds", {"auto": AUTO_THRESHOLD, "review": REVIEW_THRESHOLD})
    total    = len(auto) + len(review) + len(unmapped)

    print("\n" + "=" * 70)
    print(f"  SEMANTIC MAPPING REPORT  ({total} SKUs processed)")
    print("=" * 70)

    print(f"\n[AUTO-MAPPED]  {len(auto)} SKUs  (score >= {thresh['auto']})")
    for sku, info in auto.items():
        print(f"  {sku:<12} → {info['class']:<18} ({info['score']:.2f})  {info['name']}")

    print(f"\n[REVIEW QUEUE]  {len(review)} SKUs  ({thresh['review']} <= score < {thresh['auto']})")
    for item in review:
        print(f"  {item['sku']:<12} → {item['best_class']:<18} ({item['score']:.2f})  {item['name']}")

    print(f"\n[UNMAPPED]  {len(unmapped)} SKUs  (score < {thresh['review']})")
    for item in unmapped:
        print(f"  {item['sku']:<12} → {item['best_class']:<18} ({item['score']:.2f})  {item['name']}")
        print(f"             ** AI is not trained to detect this instrument **")

    print("\n" + "=" * 70)
    auto_pct = len(auto) / total * 100 if total else 0
    print(f"  Auto-mapped: {len(auto)}/{total} ({auto_pct:.0f}%)")
    print("=" * 70 + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Semantic SKU → SurgeoNet class mapper (multilingual)"
    )
    parser.add_argument("--demo",   action="store_true",
                        help="Run with built-in Rhosse/Edlo/Bahadir demo catalog")
    parser.add_argument("--input",  type=Path,
                        help="Path to input catalog JSON file")
    parser.add_argument("--output", type=Path, default=None,
                        help="Path to write output JSON (default: print only)")
    parser.add_argument("--merge",  type=Path, default=None,
                        help="Merge auto-mapped results into existing mapping JSON")
    parser.add_argument("--no-translate", action="store_true",
                        help="Skip translation step (English-only catalogs)")
    parser.add_argument("--threshold-auto",   type=float, default=AUTO_THRESHOLD,
                        help=f"Auto-map threshold (default: {AUTO_THRESHOLD})")
    parser.add_argument("--threshold-review", type=float, default=REVIEW_THRESHOLD,
                        help=f"Review threshold (default: {REVIEW_THRESHOLD})")
    args = parser.parse_args()

    auto_thresh   = args.threshold_auto
    review_thresh = args.threshold_review

    if args.demo:
        catalog = DEMO_CATALOG
    elif args.input:
        catalog = json.loads(args.input.read_text())
    else:
        parser.print_help()
        sys.exit(1)

    model = load_model()
    anchor_embeddings = build_anchor_embeddings(model)
    results = map_catalog(catalog, model, anchor_embeddings, auto_thresh, review_thresh,
                          skip_translation=args.no_translate)

    print_report(results)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, indent=2, ensure_ascii=False))
        print(f"Results written to {args.output}")

    if args.merge:
        existing: dict = {}
        if args.merge.exists():
            existing = json.loads(args.merge.read_text())
        existing.setdefault("auto_mapped", {}).update(results["auto_mapped"])
        args.merge.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
        print(f"Auto-mapped results merged into {args.merge}")


if __name__ == "__main__":
    main()
