"""Seed the SQLite database from existing upgrade-tracker.html files.

Usage:
    python3 seed.py                              # Import from upgrade-tracker.html
    python3 seed.py rel62-upgrade-tracker.html    # Import from specific file
    python3 seed.py --json data.json              # Import from raw JSON array
"""

import sys
import os
import re
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import init_db, get_connection, insert_release, insert_event, update_release_dates, detect_release

def extract_raw_data_from_html(html_path):
    """Extract the RAW_DATA JSON array from an HTML file."""
    with open(html_path, 'r') as f:
        content = f.read()
    m = re.search(r'const RAW_DATA = (\[.*?\]);\s*\n', content, re.DOTALL)
    if not m:
        print(f"ERROR: Could not find RAW_DATA in {html_path}")
        sys.exit(1)
    return json.loads(m.group(1))


def seed_events(events):
    """Insert a list of event dicts into the database, auto-detecting releases."""
    conn = get_connection()
    release_cache = {}
    count = 0

    for ev in events:
        rel_name = detect_release(ev.get('summary', ''))
        if not rel_name:
            continue

        if rel_name not in release_cache:
            release_cache[rel_name] = insert_release(conn, rel_name)

        insert_event(conn, release_cache[rel_name], ev)
        count += 1

    # Update date ranges for each release
    for rel_name, rel_id in release_cache.items():
        update_release_dates(conn, rel_id)

    conn.commit()
    conn.close()

    print(f"Seeded {count} events across {len(release_cache)} release(s): {', '.join(sorted(release_cache.keys()))}")


def main():
    init_db()

    if len(sys.argv) > 1 and sys.argv[1] == '--json':
        json_path = sys.argv[2] if len(sys.argv) > 2 else 'events.json'
        with open(json_path) as f:
            events = json.load(f)
    else:
        html_path = sys.argv[1] if len(sys.argv) > 1 else 'upgrade-tracker.html'
        if not os.path.exists(html_path):
            print(f"ERROR: {html_path} not found")
            sys.exit(1)
        events = extract_raw_data_from_html(html_path)

    print(f"Found {len(events)} events to import")
    seed_events(events)

    # Show summary
    conn = get_connection()
    for row in conn.execute("SELECT name, (SELECT COUNT(*) FROM events WHERE release_id = releases.id) as cnt FROM releases"):
        tenants = conn.execute("""
            SELECT COUNT(DISTINCT et.tenant_id)
            FROM event_tenants et
            JOIN events e ON e.id = et.event_id
            WHERE e.release_id = (SELECT id FROM releases WHERE name = ?)
        """, (row['name'],)).fetchone()[0]
        print(f"  {row['name']}: {row['cnt']} events, {tenants} tenants")
    conn.close()


if __name__ == '__main__':
    main()
