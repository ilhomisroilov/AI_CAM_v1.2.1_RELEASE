"""Import human-reviewed collection items into the dataset.

Only explicit human ACCEPT decisions become KNOWN. REJECT/UNCERTAIN stay out of KNOWN.
Already-imported rows (review_status=IMPORTED_TO_DATASET) are never imported twice.
Returns the number of newly imported items. Registry update + split rebuild are separate
steps (tools/rebuild_registry.py, tools/build_production_split.py).
"""
from __future__ import annotations
import argparse, csv, os, sqlite3


def import_accepted(db_path: str, known_manifest: str) -> dict:
    conn = sqlite3.connect(db_path)
    imported = 0; skipped_dup = 0
    try:
        cur = conn.execute(
            "SELECT collection_id, expected_character, character_crop_path, session_id, "
            "expected_vin, character_position, source_hash, crop_hash "
            "FROM ocr_collection_items WHERE review_status='ACCEPTED' "
            "AND label_status='TRUSTED_LABEL'")
        rows = cur.fetchall()
        # existing known crop identities (avoid double import)
        existing = set()
        if os.path.isfile(known_manifest):
            for r in csv.DictReader(open(known_manifest, newline='', encoding='utf-8')):
                existing.add(os.path.splitext(os.path.basename(r.get("crop_path", "")))[0])
        new_rows = []
        for cid, ch, crop, sess, vin, pos, shash, chash in rows:
            key = os.path.splitext(os.path.basename(crop or ""))[0]
            if key in existing:
                skipped_dup += 1; continue
            if not ch or ch not in "0123456789ABCDEFHJNST":   # unsupported (e.g. G/V) never enter KNOWN
                continue
            new_rows.append({
                "accepted_status": "human_verified", "character_label": ch, "crop_path": crop,
                "source_line_id": sess, "source_full_line_label": vin or "",
                "character_position": pos, "crop_sha256": chash, "duplicate_group_id": shash,
                "reviewer_or_decision_source": "human_review", "extraction_method": "active_learning_collector",
            })
            existing.add(key)
        if new_rows:
            # append to known_manifest (create with union header if new)
            file_exists = os.path.isfile(known_manifest)
            fieldnames = None
            if file_exists:
                with open(known_manifest, newline='', encoding='utf-8') as f:
                    fieldnames = next(csv.reader(f), None)
            if not fieldnames:
                fieldnames = list(new_rows[0].keys())
            with open(known_manifest, "a", newline='', encoding='utf-8') as f:
                w = csv.DictWriter(f, fieldnames=fieldnames)
                if not file_exists:
                    w.writeheader()
                for r in new_rows:
                    w.writerow({k: r.get(k, "") for k in fieldnames})
            # mark imported (idempotent)
            ids = [r_id for (r_id, *_rest) in rows]
            conn.executemany("UPDATE ocr_collection_items SET review_status='IMPORTED_TO_DATASET' "
                             "WHERE collection_id=?", [(i,) for i in ids])
            conn.commit()
            imported = len(new_rows)
    finally:
        conn.close()
    return {"imported": imported, "skipped_duplicate": skipped_dup}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--known", default="character_dataset/manifests/known_manifest.csv")
    a = ap.parse_args()
    print(import_accepted(a.db, a.known))


if __name__ == "__main__":
    main()
