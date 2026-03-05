# Upgrade Tracker — EC2 Deployment Guide

Deploy the Production Upgrade Tracker to Deepesh's EC2 server running Ubuntu + nginx.

## Architecture

```
Internet
   |
   v
nginx (port 80/443)
   |--- tracker.tools.alkira.cc  -->  localhost:8080 (upgrade-tracker)
   |--- tools.alkira.cc          -->  localhost:XXXX (Deepesh's existing tool)
```

- Separate Python process, separate systemd service
- Own SQLite database, own OAuth2 tokens
- No impact on the existing tool

## Prerequisites

- Ubuntu EC2 instance with nginx running
- Python 3.10+ installed (`python3 --version`)
- DNS: `tracker.tools.alkira.cc` A-record pointing to the EC2 public IP
- SSH access with sudo privileges

## Google Cloud Console Setup

Before deploying, set up OAuth2 credentials that work with the server's domain:

1. Go to [Google Cloud Console](https://console.cloud.google.com/) > **APIs & Services**
2. Ensure **Google Calendar API** is enabled (Library > search "Calendar" > Enable)
3. Go to **Credentials** > click on your existing OAuth client (or create a new one)
4. Application type: **Web application**
5. Under **Authorized redirect URIs**, add:
   - `http://tracker.tools.alkira.cc/oauth/callback`
   - (Keep `http://localhost:8080/oauth/callback` for local development)
6. Save and note the **Client ID** and **Client Secret**

## Deployment Steps

### 1. Copy files to the server

```bash
scp -i <key.pem> -r deployment-deepesh-server/ <user>@<ec2-ip>:/tmp/tracker-deploy/
```

### 2. SSH in and run the deploy script

```bash
ssh -i <key.pem> <user>@<ec2-ip>
cd /tmp/tracker-deploy
sudo bash deploy.sh
```

The script will:
- Create a `tracker` system user (no login shell)
- Deploy app files to `/opt/upgrade-tracker/`
- Create an environment file at `/etc/upgrade-tracker.env`
- Set up a systemd service (`upgrade-tracker.service`)
- Configure nginx reverse proxy for `tracker.tools.alkira.cc`
- Initialize the SQLite database
- Start the service

### 3. Set OAuth2 credentials

```bash
sudo nano /etc/upgrade-tracker.env
```

Update these values with your Google Cloud OAuth2 credentials:

```
GCAL_CLIENT_ID=1096783983162-xxxxx.apps.googleusercontent.com
GCAL_CLIENT_SECRET=GOCSPX-xxxxx
GCAL_REDIRECT_URI=http://tracker.tools.alkira.cc/oauth/callback
GCAL_TOKEN_PATH=/opt/upgrade-tracker/.gcal_token.json
```

Then restart:

```bash
sudo systemctl restart upgrade-tracker
```

### 4. First-time authorization

1. Open `http://tracker.tools.alkira.cc` in your browser
2. Click **"Fetch New Release"**
3. Google login page opens — sign in with an account that has access to the Production Upgrade calendar
4. Grant read-only calendar access
5. You'll see "Authorized!" — close that tab
6. Click **"Fetch New Release"** again — data is pulled from the calendar

The OAuth token is saved to `/opt/upgrade-tracker/.gcal_token.json` and will auto-refresh. No re-login needed.

### 5. Optional: HTTPS with Let's Encrypt

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d tracker.tools.alkira.cc
```

After setting up HTTPS, update the redirect URI:

1. In `/etc/upgrade-tracker.env`, change:
   ```
   GCAL_REDIRECT_URI=https://tracker.tools.alkira.cc/oauth/callback
   ```
2. In Google Cloud Console, add `https://tracker.tools.alkira.cc/oauth/callback` to Authorized redirect URIs
3. Delete the old token to force re-auth: `sudo rm /opt/upgrade-tracker/.gcal_token.json`
4. Restart: `sudo systemctl restart upgrade-tracker`

## Managing the Service

```bash
# Status
sudo systemctl status upgrade-tracker

# Restart
sudo systemctl restart upgrade-tracker

# Stop
sudo systemctl stop upgrade-tracker

# View logs (live)
sudo journalctl -u upgrade-tracker -f

# View last 50 log lines
sudo journalctl -u upgrade-tracker -n 50
```

## Updating the Application

When you have code changes, use the update script (preserves config, database, and tokens):

```bash
# From your local machine
scp -i <key.pem> -r deployment-deepesh-server/ <user>@<ec2-ip>:/tmp/tracker-deploy/

# On the server
cd /tmp/tracker-deploy
sudo bash update.sh
```

## Deploying with Claude

If Deepesh (or anyone with SSH access) wants to use Claude to deploy or troubleshoot:

```
Prompt: "Deploy the upgrade tracker from /tmp/tracker-deploy/ to this Ubuntu EC2 server.
         It has nginx already running for tools.alkira.cc.
         Run: sudo bash /tmp/tracker-deploy/deploy.sh
         Then edit /etc/upgrade-tracker.env with the OAuth2 credentials below:
         GCAL_CLIENT_ID=<paste>
         GCAL_CLIENT_SECRET=<paste>
         Then restart: sudo systemctl restart upgrade-tracker"
```

For troubleshooting:

```
Prompt: "The upgrade tracker at tracker.tools.alkira.cc is not loading.
         Check: sudo systemctl status upgrade-tracker
         And: sudo journalctl -u upgrade-tracker -n 50
         And: sudo nginx -t"
```

## File Locations on the Server

| Path | Purpose |
|------|---------|
| `/opt/upgrade-tracker/` | Application directory |
| `/opt/upgrade-tracker/server.py` | HTTP server + API |
| `/opt/upgrade-tracker/db.py` | Database layer |
| `/opt/upgrade-tracker/gcal.py` | Google Calendar OAuth2 fetcher |
| `/opt/upgrade-tracker/tracker.db` | SQLite database |
| `/opt/upgrade-tracker/.gcal_token.json` | OAuth2 refresh token |
| `/opt/upgrade-tracker/static/index.html` | Frontend UI |
| `/etc/upgrade-tracker.env` | Environment variables (credentials) |
| `/etc/systemd/system/upgrade-tracker.service` | systemd service |
| `/etc/nginx/sites-available/upgrade-tracker` | nginx config |

## Troubleshooting

### Service won't start
```bash
sudo journalctl -u upgrade-tracker -n 50
# Common: Python syntax error, missing file, port conflict
```

### "Fetch New Release" returns error
```bash
# Check if credentials are set
sudo cat /etc/upgrade-tracker.env

# Check if token exists
ls -la /opt/upgrade-tracker/.gcal_token.json

# Delete token to force re-auth
sudo rm /opt/upgrade-tracker/.gcal_token.json
sudo systemctl restart upgrade-tracker
```

### nginx 502 Bad Gateway
```bash
# Check if the Python process is running
sudo systemctl status upgrade-tracker

# Check if it's listening on the right port
curl http://127.0.0.1:8080/api/releases
```

### Port 8080 conflict
If another service uses port 8080, change it in:
1. `/etc/systemd/system/upgrade-tracker.service` — the `--port` flag
2. `/etc/nginx/sites-available/upgrade-tracker` — the `proxy_pass` line
3. Then: `sudo systemctl daemon-reload && sudo systemctl restart upgrade-tracker && sudo systemctl reload nginx`
