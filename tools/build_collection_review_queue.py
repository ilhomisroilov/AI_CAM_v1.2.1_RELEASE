"""Build a glyph-review queue from PENDING active-learning collection items.

Selects ocr_collection_items with review_status=PENDING, prioritizes V/G/A/D/H/B/5/6/7,
preserves session + provenance, excludes already-imported items, and writes a queue CSV
compatible with the existing keyboard glyph review app (tools/glyph_review_app/server.py
--queue ...).
"""
from __future__ import annotations
import argparse, csv, os, sqlite3, ntpath

PRIORITY = "VGADHB567023"

def build(db_path: str, out_csv: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT * FROM ocr_collection_items WHERE review_status='PENDING' "
            "AND review_status != 'IMPORTED_TO_DATASET'")
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()
    def rank(r):
        ch = (r.get("expected_character") or "")
        return (PRIORITY.index(ch) if ch in PRIORITY else 99, -(r.get("model_confidence") or 0))
    rows.sort(key=rank)
    fields = ["candidate_id", "source_line_id", "normalized_image_path", "verified_full_line_label",
              "expected_char", "character_position", "orig_x", "orig_y", "orig_width", "orig_height",
              "image_width", "image_height", "crop_path", "duplicate_group_id", "ai_advisory_decision",
              "ai_advisory_confidence", "confusion_risk", "quality_score", "queue_rank", "collection_id"]
    os.makedirs(os.path.dirname(out_csv) or ".", exist_ok=True)
    with open(out_csv, "w", newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
        for i, r in enumerate(rows, 1):
            import json as _j
            box = {}
            try: box = _j.loads(r.get("crop_box_json") or "{}")
            except Exception: box = {}
            w.writerow({
                "candidate_id": r["collection_id"], "source_line_id": r.get("session_id", ""),
                "normalized_image_path": r.get("normalized_line_path", ""),
                "verified_full_line_label": r.get("expected_vin", ""),
                "expected_char": r.get("expected_character") or "?",
                "character_position": r.get("character_position", 0),
                "orig_x": box.get("x", 0), "orig_y": box.get("y", 0),
                "orig_width": box.get("w", 0), "orig_height": box.get("h", 0),
                "image_width": 0, "image_height": 0, "crop_path": r.get("character_crop_path", ""),
                "duplicate_group_id": r.get("source_hash", ""),
                "ai_advisory_decision": r.get("model_character", ""),
                "ai_advisory_confidence": r.get("model_confidence", ""),
                "confusion_risk": 1 if (r.get("expected_character") in set("05128SB")) else 0,
                "quality_score": r.get("model_confidence", 0), "queue_rank": i,
                "collection_id": r["collection_id"]})
    print(f"collection review queue: {len(rows)} PENDING items -> {out_csv}")
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", default="character_dataset/manifests/collection_review_queue.csv")
    a = ap.parse_args()
    build(a.db, a.out)


if __name__ == "__main__":
    main()
