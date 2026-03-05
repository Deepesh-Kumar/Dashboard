"""Google Calendar fetcher for upgrade events.

Fetches events from the shared Production Upgrade calendar using OAuth2,
auto-detects REL* releases, and parses tenant/CSN data.

Environment variables:
  GCAL_CLIENT_ID       - OAuth2 client ID (Web app)
  GCAL_CLIENT_SECRET   - OAuth2 client secret

On first run, opens a browser for Google login. Saves a refresh token
to ~/.tracker_token.json so subsequent fetches are automatic.
"""

import http.server
import json
import os
import re
import threading
import urllib.parse
import urllib.request
import webbrowser

CALENDAR_ID = 'alkira.net_rkenuolllbu5laidl3rukbroec@group.calendar.google.com'
TOKEN_PATH = os.environ.get('GCAL_TOKEN_PATH', os.path.expanduser('~/.tracker_gcal_token.json'))
SCOPES = 'https://www.googleapis.com/auth/calendar.readonly'
REDIRECT_URI = os.environ.get('GCAL_REDIRECT_URI', 'http://localhost:8080/oauth/callback')


def _get_client_creds():
    client_id = os.environ.get('GCAL_CLIENT_ID')
    client_secret = os.environ.get('GCAL_CLIENT_SECRET')
    if not client_id or not client_secret:
        raise ValueError(
            'OAuth2 credentials required. Set GCAL_CLIENT_ID and GCAL_CLIENT_SECRET '
            'environment variables. See README.md for setup instructions.'
        )
    return client_id, client_secret


def _load_token():
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH) as f:
            return json.load(f)
    return None


def _save_token(token_data):
    with open(TOKEN_PATH, 'w') as f:
        json.dump(token_data, f)


def _exchange_code(code, client_id, client_secret):
    """Exchange authorization code for access + refresh tokens."""
    data = urllib.parse.urlencode({
        'code': code,
        'client_id': client_id,
        'client_secret': client_secret,
        'redirect_uri': REDIRECT_URI,
        'grant_type': 'authorization_code',
    }).encode()
    req = urllib.request.Request('https://oauth2.googleapis.com/token', data=data, method='POST')
    resp = urllib.request.urlopen(req, timeout=15)
    return json.loads(resp.read().decode())


def _refresh_access_token(refresh_token, client_id, client_secret):
    """Use refresh token to get a new access token."""
    data = urllib.parse.urlencode({
        'refresh_token': refresh_token,
        'client_id': client_id,
        'client_secret': client_secret,
        'grant_type': 'refresh_token',
    }).encode()
    req = urllib.request.Request('https://oauth2.googleapis.com/token', data=data, method='POST')
    resp = urllib.request.urlopen(req, timeout=15)
    return json.loads(resp.read().decode())


def authorize():
    """Run the OAuth2 authorization flow. Opens browser, waits for callback.
    Returns token data with access_token and refresh_token.
    """
    client_id, client_secret = _get_client_creds()

    auth_url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urllib.parse.urlencode({
        'client_id': client_id,
        'redirect_uri': REDIRECT_URI,
        'response_type': 'code',
        'scope': SCOPES,
        'access_type': 'offline',
        'prompt': 'consent',
    })

    auth_code = None
    error = None

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal auth_code, error
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if 'code' in params:
                auth_code = params['code'][0]
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.end_headers()
                self.wfile.write(b'<html><body style="background:#0f172a;color:#e2e8f0;font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh"><div style="text-align:center"><h2>Authorized!</h2><p>You can close this tab and return to the tracker.</p></div></body></html>')
            else:
                error = params.get('error', ['unknown'])[0]
                self.send_response(400)
                self.end_headers()
                self.wfile.write(f'Authorization failed: {error}'.encode())

        def log_message(self, *args):
            pass  # Suppress logs

    server = http.server.HTTPServer(('localhost', REDIRECT_PORT), CallbackHandler)
    server.timeout = 120  # 2 minute timeout

    print(f'Opening browser for Google authorization...')
    webbrowser.open(auth_url)

    server.handle_request()
    server.server_close()

    if error:
        raise RuntimeError(f'Authorization failed: {error}')
    if not auth_code:
        raise RuntimeError('Authorization timed out — no response received within 2 minutes.')

    token_data = _exchange_code(auth_code, client_id, client_secret)
    _save_token(token_data)
    print('Authorization successful! Token saved.')
    return token_data


def get_access_token():
    """Get a valid access token, refreshing or re-authorizing as needed."""
    client_id, client_secret = _get_client_creds()
    token = _load_token()

    if token and 'refresh_token' in token:
        try:
            refreshed = _refresh_access_token(token['refresh_token'], client_id, client_secret)
            # Preserve the refresh token (not always returned on refresh)
            refreshed.setdefault('refresh_token', token['refresh_token'])
            _save_token(refreshed)
            return refreshed['access_token']
        except Exception:
            pass  # Refresh failed, re-authorize

    token = authorize()
    return token['access_token']


def get_auth_url():
    """Return the OAuth2 authorization URL (for server-initiated flow)."""
    client_id, _ = _get_client_creds()
    return 'https://accounts.google.com/o/oauth2/v2/auth?' + urllib.parse.urlencode({
        'client_id': client_id,
        'redirect_uri': REDIRECT_URI,
        'response_type': 'code',
        'scope': SCOPES,
        'access_type': 'offline',
        'prompt': 'consent',
    })


def handle_oauth_callback(query_string):
    """Handle the OAuth callback, exchange code for tokens. Returns token data."""
    params = urllib.parse.parse_qs(query_string)
    if 'error' in params:
        raise RuntimeError(f"Authorization denied: {params['error'][0]}")
    if 'code' not in params:
        raise RuntimeError('No authorization code received')

    client_id, client_secret = _get_client_creds()
    token_data = _exchange_code(params['code'][0], client_id, client_secret)
    _save_token(token_data)
    return token_data


def is_authorized():
    """Check if we have a saved token."""
    token = _load_token()
    return token is not None and 'refresh_token' in token


def fetch_events(time_min=None, time_max=None):
    """Fetch all REL* events from the Production Upgrade calendar.

    Returns list of event dicts in the same format as RAW_DATA
    (summary, start, end, tenants, csns, id, htmlLink, location, status).
    """
    access_token = get_access_token()

    encoded_cal = urllib.parse.quote(CALENDAR_ID)
    base_url = f'https://www.googleapis.com/calendar/v3/calendars/{encoded_cal}/events'

    all_events = []
    page_token = None

    while True:
        params = {
            'maxResults': '2500',
            'singleEvents': 'true',
            'orderBy': 'startTime',
        }
        if time_min:
            params['timeMin'] = time_min
        if time_max:
            params['timeMax'] = time_max
        if page_token:
            params['pageToken'] = page_token

        url = base_url + '?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            'Authorization': f'Bearer {access_token}',
        })
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read().decode('utf-8'))

        for item in data.get('items', []):
            summary = item.get('summary', '')
            if not re.match(r'REL\d+', summary):
                continue

            # Parse tenants from description
            tenants_str = ''
            csns = []
            desc = item.get('description', '')
            for line in desc.split('\n'):
                line = line.strip()
                if line.startswith('Tenants:'):
                    tenants_str = line[len('Tenants:'):].strip()
                elif line.startswith('CSN-'):
                    csn_match = re.match(r'(CSN-[\w-]+)', line)
                    if csn_match:
                        csns.append(csn_match.group(1))

            start = item.get('start', {}).get('dateTime', item.get('start', {}).get('date', ''))
            end = item.get('end', {}).get('dateTime', item.get('end', {}).get('date', ''))

            all_events.append({
                'summary': summary,
                'start': start,
                'end': end,
                'tenants': tenants_str,
                'csns': csns,
                'id': item.get('id', ''),
                'htmlLink': item.get('htmlLink', ''),
                'location': item.get('location', ''),
                'status': item.get('status', 'confirmed'),
            })

        page_token = data.get('nextPageToken')
        if not page_token:
            break

    return all_events


def detect_releases(events):
    """Group events by release name. Returns {rel_name: [events]}."""
    grouped = {}
    for ev in events:
        m = re.match(r'(REL\d+)', ev.get('summary', ''))
        if m:
            grouped.setdefault(m.group(1), []).append(ev)
    return grouped


if __name__ == '__main__':
    try:
        events = fetch_events()
        releases = detect_releases(events)
        print(f'Fetched {len(events)} events across {len(releases)} releases:')
        for rel, evs in sorted(releases.items()):
            print(f'  {rel}: {len(evs)} events')
    except Exception as e:
        print(f'Error: {e}', file=sys.stderr)
        import sys
        sys.exit(1)
