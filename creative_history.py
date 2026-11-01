"""Read-only projection of Hermes creative records onto Billing Control applications."""
import csv
from collections import defaultdict
from datetime import date

CUTOFF = date(2026, 8, 1)
REQUIRED = {'billing_application_id', 'billing_app_no', 'billing_period_end', 'document_status', 'observation_id', 'parent_billing_application_id', 'place_id', 'layout_name', 'layout_asset_ids', 'placement_start', 'placement_end', 'change_date', 'change_kind', 'actual_status'}
EXCLUDED = {'withdrawn', 'not_found'}

def read_export(path):
    with open(path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f, delimiter=';')
        missing = REQUIRED - set(reader.fieldnames or ())
        if missing:
            raise ValueError('Missing columns: ' + ', '.join(sorted(missing)))
        return list(reader)

def project_history(rows):
    """Link DS creatives to their parent without altering the DS or inferring installation."""
    history = defaultdict(list)
    seen = set()
    for row in rows:
        end = row.get('billing_period_end', '')
        if not end or date.fromisoformat(end) < CUTOFF:
            continue
        if row.get('document_status') in EXCLUDED or not row.get('observation_id'):
            continue
        parent = row.get('parent_billing_application_id') or row['billing_application_id']
        key = (row['billing_application_id'], row['observation_id'])
        if key in seen:
            raise ValueError('Duplicate observation: ' + str(key))
        seen.add(key)
        history[parent].append({
            'parent_application_id': parent,
            'source_application_id': row['billing_application_id'],
            'source_document_number': row['billing_app_no'],
            'source_document_id': row.get('document_id', ''),
            'source_document_status': row['document_status'],
            'source_type': 'supplement' if row.get('parent_billing_application_id') else 'application',
            'place_id': row['place_id'],
            'place_unresolved': not bool(row['place_id']),
            'place_title': row.get('place_title_in_registry', ''),
            'layout_name': row['layout_name'],
            'layout_asset_ids': row['layout_asset_ids'],
            'planned_from': row['placement_start'],
            'planned_to': row['placement_end'],
            'change_date': row['change_date'],
            'change_kind': row['change_kind'],
            'actual_status': row['actual_status'],
            'actual_source': row.get('actual_source', ''),
            'user_confirmed_from': row.get('user_confirmed_from', ''),
            'observation_id': row['observation_id'],
        })
    for items in history.values():
        items.sort(key=lambda x: (x['place_id'], x['planned_from'] or x['change_date'], x['source_application_id']))
    return dict(history)
