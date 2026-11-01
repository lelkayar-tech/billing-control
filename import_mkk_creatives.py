"""Import validated Hermes CSV into isolated MKK table; DRY RUN by default.

Usage:
    python import_mkk_creatives.py /path/to/billing-control-mkk-creatives.csv
    python import_mkk_creatives.py /path/to/file.csv --apply

Requires SUPABASE_URL and SUPABASE_SECRET_KEY only with --apply.
Does not modify applications, periods, invoices, or payments.
"""
import argparse
import os
from collections import Counter

from creative_history import project_history, read_export


def prepare(path):
    rows = read_export(path)
    history = project_history(rows)
    payload = []
    for parent, items in history.items():
        for item in items:
            payload.append({
                "source_application_id": int(item["source_application_id"]),
                "observation_id": item["observation_id"],
                "parent_application_id": int(parent),
                "source_document_number": item["source_document_number"],
                "source_document_status": item["source_document_status"],
                "place_id": item["place_id"] or None,
                "record": item,
            })
    keys = [(r["source_application_id"], r["observation_id"]) for r in payload]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate observation keys")
    return rows, payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv")
    parser.add_argument("--apply", action="store_true", help="Explicitly write to Supabase")
    args = parser.parse_args()
    source, payload = prepare(args.csv)
    counts = Counter(r["source_document_status"] for r in payload)
    print(f"CSV rows: {len(source)}; importable observations: {len(payload)}; statuses: {dict(counts)}")
    print(f"Unique parents: {len(set(x['parent_application_id'] for x in payload))}")
    if not args.apply:
        print("DRY RUN. Database unchanged.")
        return
    from dotenv import load_dotenv
    from supabase import create_client
    load_dotenv()
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SECRET_KEY")
    if not url or not key:
        raise RuntimeError("Missing server-side Supabase credentials")
    client = create_client(url, key)
    # Fail closed: every source and parent must exist and belong to MKK.
    ids = sorted({r["source_application_id"] for r in payload} |
                 {r["parent_application_id"] for r in payload})
    allowed = set()
    for start in range(0, len(ids), 100):
        response = (client.table("applications")
                    .select("id,legal_entity_id,legal_entities(name)")
                    .in_("id", ids[start:start + 100]).execute())
        for app in response.data or []:
            entity = app.get("legal_entities") or {}
            if entity.get("name") == "МКК":
                allowed.add(int(app["id"]))
    missing = set(ids) - allowed
    if missing:
        raise ValueError("Import blocked: non-MKK or missing application IDs: " +
                         ", ".join(map(str, sorted(missing))))
    for start in range(0, len(payload), 100):
        client.table("mkk_creative_observations").upsert(
            payload[start:start + 100],
            on_conflict="source_application_id,observation_id",
        ).execute()
    print("Import completed. Existing finance tables unchanged.")


if __name__ == "__main__":
    main()
