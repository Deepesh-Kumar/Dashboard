"""Lightweight HTTP server for the Upgrade Tracker.

Usage:
    python3 server.py              # Start on localhost:8080
    python3 server.py --port 9000  # Custom port

Zero external dependencies — uses only Python stdlib.
"""

import http.server
import json
import os
import sys
import urllib.parse
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import init_db, get_releases, get_events, search_tenants_cross_release, enforce_rolling_window

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, 'static')


class TrackerHandler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        # Static files
        if path == '/' or path == '/index.html':
            self._serve_file(os.path.join(STATIC_DIR, 'index.html'), 'text/html')
        elif path.startswith('/static/'):
            safe_path = os.path.normpath(path.lstrip('/'))
            full_path = os.path.join(BASE_DIR, safe_path)
            if os.path.isfile(full_path) and os.path.commonpath([full_path, BASE_DIR]) == BASE_DIR:
                ctype = 'text/html' if full_path.endswith('.html') else 'application/octet-stream'
                self._serve_file(full_path, ctype)
            else:
                self.send_error(404)

        # API endpoints
        elif path == '/api/releases':
            self._json_response(get_releases())

        elif path == '/api/events':
            release = self._param(params, 'release')
            q = self._param(params, 'q')
            az = self._param(params, 'az')
            time_filter = self._param(params, 'time')
            self._json_response(get_events(release=release, q=q, az=az, time_filter=time_filter))

        elif path == '/api/tenants':
            q = self._param(params, 'q', '')
            if not q:
                self._json_response({})
            else:
                self._json_response(search_tenants_cross_release(q))

        elif path == '/api/stats':
            releases = get_releases()
            self._json_response({
                'total_releases': len(releases),
                'releases': releases,
            })

        elif path == '/api/auth/status':
            from gcal import is_authorized
            self._json_response({'authorized': is_authorized()})

        elif path == '/api/auth/url':
            try:
                from gcal import get_auth_url
                self._json_response({'url': get_auth_url()})
            except ValueError as e:
                self._json_response({'error': str(e)}, status=400)

        elif path == '/oauth/callback':
            # Handle OAuth redirect from Google
            try:
                from gcal import handle_oauth_callback
                handle_oauth_callback(parsed.query)
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.end_headers()
                self.wfile.write(b'<html><body style="background:#0f172a;color:#e2e8f0;font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh"><div style="text-align:center"><h2>Authorized!</h2><p>You can close this tab and click Fetch New Release again.</p></div></body></html>')
            except Exception as e:
                self.send_response(400)
                self.send_header('Content-Type', 'text/html')
                self.end_headers()
                self.wfile.write(f'<html><body><h2>Authorization failed</h2><p>{e}</p></body></html>'.encode())

        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == '/api/refresh':
            content_len = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_len) if content_len else b'{}'
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = {}

            events_to_import = []

            if 'events' in data:
                # Direct event import (from Claude/Cowork or manual paste)
                events_to_import = data['events']

            elif data.get('fetch'):
                # Fetch from Google Calendar via OAuth2
                try:
                    from gcal import fetch_events, is_authorized, get_auth_url, detect_releases
                    if not is_authorized():
                        self._json_response({
                            'needs_auth': True,
                            'auth_url': get_auth_url(),
                        })
                        return
                    all_fetched = fetch_events()
                    # Only keep the 3 most recent releases by sorting REL numbers
                    releases = detect_releases(all_fetched)
                    sorted_rels = sorted(releases.keys(), key=lambda r: int(r.replace('REL', '')), reverse=True)
                    keep = sorted_rels[:3]
                    events_to_import = []
                    for rel in keep:
                        events_to_import.extend(releases[rel])
                except ValueError as e:
                    self._json_response({
                        'error': str(e),
                        'hint': 'Set GCAL_CLIENT_ID and GCAL_CLIENT_SECRET env vars and restart the server.'
                    }, status=400)
                    return
                except Exception as e:
                    self._json_response({
                        'error': f'Failed to fetch from Google Calendar: {e}'
                    }, status=500)
                    return

            else:
                self._json_response({
                    'error': 'POST body must include "events" array or {"fetch": true}.',
                    'example': 'POST /api/refresh with {"events": [...]} or {"fetch": true}'
                }, status=400)
                return

            # Import events into DB
            from db import get_connection, insert_release, insert_event, update_release_dates, detect_release
            conn = get_connection()
            release_cache = {}
            count = 0
            for ev in events_to_import:
                rel_name = detect_release(ev.get('summary', ''))
                if not rel_name:
                    continue
                if rel_name not in release_cache:
                    release_cache[rel_name] = insert_release(conn, rel_name)
                insert_event(conn, release_cache[rel_name], ev)
                count += 1
            for rel_name, rel_id in release_cache.items():
                update_release_dates(conn, rel_id)
            conn.commit()
            conn.close()

            # Enforce rolling window
            pruned = enforce_rolling_window(3)

            self._json_response({
                'success': True,
                'events_imported': count,
                'releases_found': list(release_cache.keys()),
                'releases_pruned': pruned,
            })

        elif parsed.path == '/api/prune':
            pruned = enforce_rolling_window(3)
            self._json_response({'pruned': pruned})

        else:
            self.send_error(404)

    def _param(self, params, key, default=None):
        vals = params.get(key, [])
        return vals[0] if vals else default

    def _json_response(self, data, status=200):
        body = json.dumps(data, default=str).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, filepath, content_type):
        try:
            with open(filepath, 'rb') as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', content_type + '; charset=utf-8')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_error(404)

    def log_message(self, format, *args):
        # Quieter logging
        sys.stderr.write(f"[tracker] {args[0]}\n")


def main():
    parser = argparse.ArgumentParser(description='Upgrade Tracker Server')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--host', default='localhost')
    args = parser.parse_args()

    init_db()

    server = http.server.HTTPServer((args.host, args.port), TrackerHandler)
    print(f"Upgrade Tracker serving on http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down")
        server.server_close()


if __name__ == '__main__':
    main()
