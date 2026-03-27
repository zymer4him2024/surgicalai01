# Device Master Catalog & Mapping Specification

**Version**: 1.0  
**Issued by**: Antigravity Surgical AI  
**Audience**: Hospital Data Teams / Customer IT  

---

## Overview

This document describes how customers can supply the list of instrument classes and their corresponding metadata for mapping AI detection results to standardized product names (e.g., FDA Classification).

The system uses the **Device Master Agent** to translate raw model labels (like `forceps`) into human-readable, hospital-standardized names.

---

## Data Provisioning Options

Customers can provide the catalog data in two ways:

### Option A: Static Configuration (`labels.json`)
Ideal for isolated deployments or fixed sets of instruments.

1. Create/Modify `src/device_master/labels.json`.
2. Format:
```json
{
  "detection_label_name": {
    "fallback_name": "Standardized Name",
    "fallback_product_code": "Internal-ID-123",
    "fallback_class": "I",
    "fda_search_terms": ["term1", "term2"]
  }
}
```

### Option B: Cloud-Managed (Firebase Firestore)
Ideal for multi-site deployments with frequently updated inventories.

1. The system will sync with the `device_catalog` collection in Firestore.
2. Each document ID corresponds to the `detection_label`.
3. Fields required:
    - `device_name`: string (e.g., "Tissue Forceps, Ring")
    - `fda_class`: string ("I", "II", "III")
    - `material`: string (optional, e.g., "Stainless Steel")

---

## Required Class Mapping

The customer MUST provide mappings for every class output by the AI model. Current supported classes include:

| AI Label | Recommended Mapping (FDA Standard) |
|---|---|
| `forceps` | Tissue Forceps, Ring (Class I) |
| `scalpel` | Surgical Scalpel/Knife (Class II) |
| `scissors` | Surgical Scissors (Class I) |
| `needle_holder` | Needle Holder (Class I) |
| `clamp` | Hemostat/Vascular Clamp (Class I) |
| `retractor` | Self-retaining Retractor (Class I) |
| `suction` | Surgical Suction Tube (Class II) |

---

## Authentication & Access (Option B)

If using Option B, the customer will be provided with a **Service Account Token** to push updates to the Firestore `device_catalog` collection via the Firebase Admin SDK or REST API.

---

## Best Practices

1. **Lowercase Labels**: All AI internal labels used in the JSON keys must be lowercase.
2. **Standardization**: Use FDA UDI standards for `device_name` where possible to ensure regulatory compliance.
3. **Product Codes**: If available, include your internal hospital product codes to enable integration with hospital ERP/Inventory systems.

---

## Support

Technical Support: Antigravity Surgical AI Data Engineering Team.
