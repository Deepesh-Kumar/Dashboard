"""SQLite database layer for the Upgrade Tracker."""

import sqlite3
import os
import re

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tracker.db')

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS releases (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            date_start  TEXT,
            date_end    TEXT
        );

        CREATE TABLE IF NOT EXISTS events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            release_id  INTEGER NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
            gcal_id     TEXT NOT NULL,
            summary     TEXT NOT NULL,
            az          TEXT,
            batch_num   INTEGER,
            start_time  TEXT NOT NULL,
            end_time    TEXT NOT NULL,
            location    TEXT,
            html_link   TEXT,
            status      TEXT DEFAULT 'confirmed',
            tenants_raw TEXT,
            UNIQUE(release_id, gcal_id)
        );

        CREATE TABLE IF NOT EXISTS tenants (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE
        );

        CREATE TABLE IF NOT EXISTS event_tenants (
            event_id    INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            tenant_id   INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            status      TEXT,
            size_info   TEXT,
            PRIMARY KEY (event_id, tenant_id)
        );

        CREATE TABLE IF NOT EXISTS event_csns (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            csn      TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_events_release ON events(release_id);
        CREATE INDEX IF NOT EXISTS idx_event_csns_csn ON event_csns(csn);
        CREATE INDEX IF NOT EXISTS idx_event_csns_event ON event_csns(event_id);
        CREATE INDEX IF NOT EXISTS idx_event_tenants_tenant ON event_tenants(tenant_id);
    """)
    conn.commit()
    conn.close()


# --- Tenant string parser ---

def parse_tenant_string(raw):
    """Parse tenant string like 'rccl(PROVISIONED)(S), flex(PROVISIONED)(L)'.
    Returns list of dicts: [{name, status, size_info}, ...]
    """
    if not raw:
        return []
    results = []
    for part in raw.split(','):
        part = part.strip()
        if not part:
            continue
        m = re.match(r'^([^(]+?)(?:\((\w+)\))?(?:\((\w+)\))?$', part)
        if m:
            results.append({
                'name': m.group(1).strip(),
                'status': m.group(2),
                'size_info': m.group(3),
            })
        else:
            results.append({'name': part.strip(), 'status': None, 'size_info': None})
    return results


def extract_az(summary):
    if 'AZ0' in summary:
        return 'AZ0'
    elif 'AZ1' in summary:
        return 'AZ1'
    return 'prep'


def extract_batch_num(summary):
    m = re.search(r'Batch-(\d+)', summary)
    return int(m.group(1)) if m else None


def detect_release(summary):
    m = re.match(r'(REL\d+)', summary)
    return m.group(1) if m else None


# --- CRUD ---

def insert_release(conn, name):
    conn.execute("INSERT OR IGNORE INTO releases (name) VALUES (?)", (name,))
    return conn.execute("SELECT id FROM releases WHERE name = ?", (name,)).fetchone()['id']


def insert_event(conn, release_id, event_data):
    """Insert a single event. event_data is a dict with raw calendar fields."""
    az = extract_az(event_data['summary'])
    batch = extract_batch_num(event_data['summary'])
    conn.execute("""
        INSERT OR REPLACE INTO events
        (release_id, gcal_id, summary, az, batch_num, start_time, end_time, location, html_link, status, tenants_raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        release_id,
        event_data.get('id', ''),
        event_data['summary'],
        az,
        batch,
        event_data.get('start', ''),
        event_data.get('end', ''),
        event_data.get('location', ''),
        event_data.get('htmlLink', ''),
        event_data.get('status', 'confirmed'),
        event_data.get('tenants', ''),
    ))
    event_id = conn.execute(
        "SELECT id FROM events WHERE release_id = ? AND gcal_id = ?",
        (release_id, event_data.get('id', ''))
    ).fetchone()['id']

    # Clear old tenant/csn links (for refresh upsert)
    conn.execute("DELETE FROM event_tenants WHERE event_id = ?", (event_id,))
    conn.execute("DELETE FROM event_csns WHERE event_id = ?", (event_id,))

    # Insert tenants
    for t in parse_tenant_string(event_data.get('tenants', '')):
        conn.execute("INSERT OR IGNORE INTO tenants (name) VALUES (?)", (t['name'],))
        tid = conn.execute("SELECT id FROM tenants WHERE name = ? COLLATE NOCASE", (t['name'],)).fetchone()['id']
        conn.execute(
            "INSERT OR IGNORE INTO event_tenants (event_id, tenant_id, status, size_info) VALUES (?, ?, ?, ?)",
            (event_id, tid, t['status'], t['size_info'])
        )

    # Insert CSNs
    for csn in event_data.get('csns', []):
        conn.execute("INSERT INTO event_csns (event_id, csn) VALUES (?, ?)", (event_id, csn))

    return event_id


def update_release_dates(conn, release_id):
    conn.execute("""
        UPDATE releases SET
            date_start = (SELECT MIN(start_time) FROM events WHERE release_id = ?),
            date_end = (SELECT MAX(end_time) FROM events WHERE release_id = ?)
        WHERE id = ?
    """, (release_id, release_id, release_id))


def enforce_rolling_window(max_releases=3):
    """Keep only the N most recent releases. Returns list of pruned release names."""
    conn = get_connection()
    releases = conn.execute(
        "SELECT id, name FROM releases ORDER BY date_start DESC"
    ).fetchall()
    pruned = []
    if len(releases) > max_releases:
        for rel in releases[max_releases:]:
            conn.execute("DELETE FROM releases WHERE id = ?", (rel['id'],))
            pruned.append(rel['name'])
        # Clean up orphaned tenants (no event_tenants links left)
        conn.execute("""
            DELETE FROM tenants WHERE id NOT IN (
                SELECT DISTINCT tenant_id FROM event_tenants
            )
        """)
        conn.commit()
    conn.close()
    return pruned


# --- Query functions ---

def get_releases():
    conn = get_connection()
    rows = conn.execute("""
        SELECT r.name,
               COUNT(DISTINCT e.id) as event_count,
               COUNT(DISTINCT et.tenant_id) as tenant_count,
               r.date_start, r.date_end
        FROM releases r
        LEFT JOIN events e ON e.release_id = r.id
        LEFT JOIN event_tenants et ON et.event_id = e.id
        GROUP BY r.id
        ORDER BY r.date_start DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_events(release=None, q=None, az=None, time_filter=None):
    conn = get_connection()
    conditions = []
    params = []

    if release:
        conditions.append("r.name = ?")
        params.append(release)

    if az:
        conditions.append("e.az = ?")
        params.append(az)

    if time_filter == 'upcoming':
        conditions.append("e.start_time > datetime('now')")
    elif time_filter == 'past':
        conditions.append("e.start_time <= datetime('now')")

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    # Get events
    sql = f"""
        SELECT DISTINCT e.id, e.gcal_id, e.summary, e.az, e.batch_num,
               e.start_time, e.end_time, e.location, e.html_link, e.status,
               e.tenants_raw, r.name as release_name
        FROM events e
        JOIN releases r ON r.id = e.release_id
        LEFT JOIN event_tenants et ON et.event_id = e.id
        LEFT JOIN tenants t ON t.id = et.tenant_id
        LEFT JOIN event_csns ec ON ec.event_id = e.id
        {where}
        ORDER BY e.start_time
    """
    events = conn.execute(sql, params).fetchall()

    # Deduplicate (joins can cause dupes)
    seen = set()
    unique_events = []
    for e in events:
        if e['id'] not in seen:
            seen.add(e['id'])
            unique_events.append(e)

    # Build result with nested tenants and CSNs
    result = []
    for e in unique_events:
        eid = e['id']
        tenants = conn.execute("""
            SELECT t.name, et.status, et.size_info
            FROM event_tenants et JOIN tenants t ON t.id = et.tenant_id
            WHERE et.event_id = ?
        """, (eid,)).fetchall()
        csns = conn.execute(
            "SELECT csn FROM event_csns WHERE event_id = ?", (eid,)
        ).fetchall()

        event_dict = dict(e)
        event_dict['tenants'] = [dict(t) for t in tenants]
        event_dict['csns'] = [c['csn'] for c in csns]
        result.append(event_dict)

    # Apply text search filter in Python (simpler than complex SQL with joins)
    if q:
        q_lower = q.lower()
        filtered = []
        for ev in result:
            searchable = (
                ev['summary'] + ' ' +
                ev.get('tenants_raw', '') + ' ' +
                ' '.join(ev['csns'])
            ).lower()
            if q_lower in searchable:
                filtered.append(ev)
        result = filtered

    conn.close()
    return result


def search_tenants_cross_release(q):
    """Search for a tenant across all releases."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT t.name as tenant_name, r.name as release_name,
               e.summary, e.start_time, e.az, e.batch_num,
               et.status, et.size_info, e.html_link
        FROM tenants t
        JOIN event_tenants et ON et.tenant_id = t.id
        JOIN events e ON e.id = et.event_id
        JOIN releases r ON r.id = e.release_id
        WHERE t.name LIKE ?
        ORDER BY t.name, r.name, e.start_time
    """, (f'%{q}%',)).fetchall()
    conn.close()

    # Group by tenant, then by release
    grouped = {}
    for row in rows:
        r = dict(row)
        tname = r['tenant_name']
        rname = r['release_name']
        if tname not in grouped:
            grouped[tname] = {}
        if rname not in grouped[tname]:
            grouped[tname][rname] = []
        grouped[tname][rname].append({
            'summary': r['summary'],
            'start_time': r['start_time'],
            'az': r['az'],
            'batch_num': r['batch_num'],
            'status': r['status'],
            'size_info': r['size_info'],
            'html_link': r['html_link'],
        })

    return grouped
