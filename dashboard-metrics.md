# Dashboard Metrics Project

## Architecture
Static HTML dashboard collecting Alkira tenant metrics from Selector AI MCP API → generates static HTML → served via Nginx on `dashboard.alkira.cc` with Okta SSO + local credential fallback.
- **Auth**: Dual auth — Okta SSO via oauth2-proxy (primary) + local cookie fallback. nginx `auth_request` to `/_auth_check` checks local cookie first, then proxies to oauth2-proxy.
- **Nginx config**: `/etc/nginx/conf.d/dashboard.conf` (HTTPS + dual auth + `/api/` proxy)
- **Dashboard API**: Small Python stdlib HTTP server (`api.py`) on port 5000 — serves editable data (feature requests links)

## Dashboard EC2 Server
| Property | Value |
|----------|-------|
| Instance ID | `i-0beda96326bb4fed8` |
| Instance Name | `SE/SA Customer Dashboard - Do not Stop or Delete` |
| Elastic IP | `54.190.170.4` |
| Region | `us-west-2` (Oregon) |
| Instance Type | `t3.micro` |
| SSH User | `ubuntu` |
| SSH Key | `~/Downloads/aws-keys/deepesh-us-west-2.pem` |
| Remote Dir | `/opt/dashboard` |
| Web Root | `/var/www/dashboard` |
| Domain | `dashboard.alkira.cc` |
| HTTPS | Let's Encrypt cert via Certbot |
| Basic Auth | `dashboard` / `Alkira2026` (local fallback) |
| Tags | Name=SE/SA Customer Dashboard - Do not Stop or Delete, DoNotDelete=true, Forever=True, NotDelete=True, ResourceOwner=deepesh@alkira.net |
| Termination Protection | Enabled |
| Stop Protection | Enabled |
| Monthly Cost | ~$10/month (t3.micro + 20GB gp3 + Elastic IP) |

## Okta SSO
| Property | Value |
|----------|-------|
| Domain | `alkira.okta.com` |
| Client ID | `0oa21hpdz391wUqQL1d8` |
| Allowed email domain | `@alkira.com` |
| Redirect URI | `https://dashboard.alkira.cc/oauth2/callback` |
| OIDC Issuer | `https://alkira.okta.com` (org-level, not `/oauth2/default`) |
| oauth2-proxy config | `/etc/oauth2-proxy/oauth2-proxy.cfg` |
| oauth2-proxy service | `systemctl status oauth2-proxy` |

**Okta App Tile**: Add as a Bookmark App in Okta Admin → Applications → Browse App Catalog → "Bookmark App" with URL `https://dashboard.alkira.cc/oauth2/start?rd=/home.html`

## Security Group
| Port | Allowed IPs |
|------|-------------|
| 22 | Corp IPs only (dynamically opened by GitHub Actions during deploy) |
| 80 | Corp IPs only |
| 443 | Corp IPs only |

Security Group ID: `sg-064f95d5e864d5d32`

## Components
- `src/collector.py` - Fetches data from Selector MCP (Streamable HTTP transport), runs 7 S2QL queries
- `src/generator.py` - Reads `latest.json`, generates index + per-tenant HTML via Jinja2
- `src/api.py` - Dashboard API server (port 5000) for editable data — feature request links
- `src/cxp_mapping.py` - CXP→cloud mapping, connector tag→type mapping, service tag→name mapping
- `src/config.py` - Config (Selector API URL/key, paths)
- `templates/` - base.html, home.html (portal), index.html (tenant table), tenant.html (detail), login-choice.html, login.html, account-mapping.html
- `static/` - dashboard.js, style.css
- `nginx/dashboard.conf` - Nginx HTTPS config with dual auth + API proxy
- `deploy/oauth2-proxy.cfg` - oauth2-proxy config template
- `deploy/dashboard-api.service` - systemd service for API server
- `deploy/setup-okta.sh` - One-time Okta setup script
- `data/feature_requests.json` - Editable feature request links (edit via UI at home.html)

## Data Files
- `data/latest.json` - Fresh from Selector MCP API (315 tenants, 107 parent tenants on index)
- `data/feature_requests.json` - Editable links shown in Feature Requests card on home page
- Selector MCP API (`alkira-prod.selector.ai/mcp`) — only reachable from corp network/VPN (not from EC2)

## GitHub Repo
- **Repo**: `https://github.com/Deepesh-Kumar/Dashboard` (private)
- **Workflow**: `.github/workflows/refresh.yml` — runs daily at 2AM UTC
- **IAM User**: `github-actions-dashboard` — minimal permissions (modify SG rules in us-west-2 only)

## GitHub Secrets Required
| Secret | Purpose |
|--------|---------|
| `AWS_ACCESS_KEY_ID` | IAM user key for dynamic SG rule management |
| `AWS_SECRET_ACCESS_KEY` | IAM user secret |
| `EC2_SSH_KEY` | Contents of `deepesh-us-west-2.pem` for rsync deploy |
| `SELECTOR_AI_API_KEY` | Selector MCP API key |

## Deployment (Local → EC2)
```bash
# 1. Collect fresh data (must run locally — Selector API unreachable from EC2)
DASHBOARD_LOCAL=1 python3 src/collector.py

# 2. Generate HTML
DASHBOARD_LOCAL=1 python3 src/generator.py

# 3. Deploy to EC2
rsync -az --delete -e "ssh -i ~/Downloads/aws-keys/deepesh-us-west-2.pem -o StrictHostKeyChecking=no" \
  output/ ubuntu@54.190.170.4:/opt/dashboard/output/
rsync -az -e "ssh -i ~/Downloads/aws-keys/deepesh-us-west-2.pem -o StrictHostKeyChecking=no" \
  static/ ubuntu@54.190.170.4:/opt/dashboard/static/
ssh -i ~/Downloads/aws-keys/deepesh-us-west-2.pem ubuntu@54.190.170.4 \
  "sudo rsync -a --delete /opt/dashboard/output/ /var/www/dashboard/ && \
   sudo rsync -a /opt/dashboard/static/ /var/www/dashboard/static/"
```

## Home Page Cards
| Card | Type | Link/Notes |
|------|------|------------|
| Tenant Metrics Dashboard | Internal | `/index.html` |
| Account Mapping | Internal | `/account-mapping.html` |
| Tenant & Connector Limit / SLAs | External | Google Sheets + Google Doc |
| CXP Regions | External | Google Sheets |
| Feature Requests | Editable | Jira dashboards — edit via ✏️ button on UI, saved to `data/feature_requests.json` via API |
| Salesforce | Coming Soon | — |
| More Tools | Coming Soon | — |

---

## Completed Changes (2026-03-05)

### HTTPS + Okta SSO on EC2
- Obtained Let's Encrypt TLS cert for `dashboard.alkira.cc` (points to `54.190.170.4`)
- Installed oauth2-proxy v7.6.0 for Okta OIDC — uses org-level issuer `https://alkira.okta.com`
- Dual auth: Okta primary, local cookie (`alkira_session=alkira_dashboard_auth`) as last resort
- nginx `auth_request /_auth_check` — checks local cookie first, then proxies to oauth2-proxy:4180
- Login choice page at `/login-choice.html` — "Sign in with Okta SSO" + "Use local credentials"
- Fixed: email domain was `alkira.net`, corrected to `alkira.com`
- Fixed: oauth2-proxy v7.x config uses underscores (not hyphens), `email_domains` (plural list), cookie_secret must be exactly 32 chars

### EC2 Instance Hardening
- Renamed instance to `SE/SA Customer Dashboard - Do not Stop or Delete`
- Enabled API Stop Protection (termination protection was already on)
- Added tags: `DoNotDelete=true`, `Forever=True`, `NotDelete=True`
- Security group locked to corp IPs only on all ports
- Added `50.204.236.130` to security group (ports 22, 80, 443)

### GitHub Actions CI/CD
- Repo pushed to `https://github.com/Deepesh-Kumar/Dashboard`
- Daily workflow at 2AM UTC: collect → generate → deploy to EC2
- Deploy uses dynamic SG IP allowlisting (no permanent SSH exposure):
  1. Get runner public IP
  2. Add to SG port 22
  3. rsync deploy
  4. Remove from SG (`if: always()` ensures cleanup even on failure)
- IAM user `github-actions-dashboard` created with minimal permissions (SG modify, us-west-2 only)
- **Known issue**: Selector API unreachable from GitHub Actions runners — collector step hangs. Pending resolution (whitelist EC2/GH runner IPs with Selector team)

### Removed GCS/Firebase
- Removed `STATIC_BASE`, `FIREBASE_CONFIG`, `OKTA_ENABLED` from config and generator
- Removed Firebase SDK from templates, firebase-auth.js, login-firebase.html, firebase.json
- All paths now root-relative (EC2 nginx serves from `/`)
- Logout always uses `/oauth2/sign_out` (Okta)

### Dashboard API for Editable Data
- `src/api.py` — Python stdlib HTTP server on `127.0.0.1:5000`
- Endpoints: `GET /api/feature-requests`, `POST /api/feature-requests`
- nginx proxies `/api/` → port 5000 (protected by Okta auth)
- Systemd service: `dashboard-api.service`

### Home Page Updates
- Renamed "Alkira Tenant Dashboard" → "Main Dashboard" (nav + page title)
- Renamed "Tenant Dashboard" card → "Tenant Metrics Dashboard"
- Removed "Dashboard Portal" heading and "Alkira network metrics and insights" subtitle
- Removed footer line "Alkira Tenant Metrics Dashboard — Data collected monthly from Selector API"
- Added **Feature Requests** card — editable via ✏️ button, links saved server-side
- Added **Tenant & Connector Limit / SLAs** card (merged from two separate cards) — links to Google Sheets + Google Doc
- Added **CXP Regions** card — links to Google Sheets
- Feature Requests links stored in `data/feature_requests.json` (committed to GitHub, editable via UI)

---

## Completed Changes (Prior Sessions)

### Collector: Streamable HTTP MCP Protocol (2026-02-16)
- Rewrote from old SSE protocol to Streamable HTTP MCP protocol (POST `/mcp/`)
- Protocol: POST `initialize` → get `mcp-session-id` header → send `notifications/initialized` → POST `tools/call`

### Collector: 7 S2QL Queries
1. Tenant list (`query_selector_tsdb_label_values` for `tenant_prefix`)
2. BW RX per CXP (`s2_connector_bw_cxp_tenant_con_rx_avg_rollup`)
3. BW TX per CXP (`s2_connector_bw_cxp_tenant_con_tx_avg_rollup`)
4. Connector health (`s2_connector_health` group-by tag, tenant, cxp_name)
5. BW utilization (`s2_connector_bw_pct` group-by name, cxp_name, tenant, size, csn_name, type)
6. Connector inventory (`s2_connector_bw_pct` group-by connectorId, connectorName, tenant_prefix, tenant, cxp_name, name, type)
7. Data egress bytes (`s2_connector_bw_tenant_bytes`)

### Generator: Sub-Tenant Merging
- Merges sub-tenant health/utilization/connector data into parent tenants
- Sub-tenants filtered from index page (315 total → 107 parent tenants shown)

### Connector Counting
- Source of truth: `connector_list` from bw_pct (unique `connectorId`)
- Tunnel-to-connector ratios: Direct Connect 2:1, Express Route 3:1, IPSec Advanced 4:1, IPSec 2:1, SD-WAN 2:1, Aruba Edge 2:1

### Cloud / On-Prem Connector Split
- Cloud: AWS VPC, Azure VNet, GCP VPC, OCI VCN, AWS TGW
- On-prem: Direct Connect, Express Route, IPSec, IPSec Advanced, SD-WAN variants, Aruba Edge

### BW Violators
- Counts CXPs where peak RX > 150% of allocated CXP BW
- Peak derived from `bw_rx_timeseries` (30-day hourly max)

### Tenant Name Cleanup
- Strips numeric suffixes, name override dict for truncated names

---

## Open Issues

### Collector Cannot Run on EC2 or GitHub Actions
- Selector MCP API (`alkira-prod.selector.ai`) unreachable from EC2 and GitHub Actions runners
- Collector must run locally on corp network/VPN
- **Action needed**: Whitelist EC2 IP `54.190.170.4` with Selector team to enable full automation

### Segment Names Not Available
- Selector TSDB has no segment name data — only numeric IDs
- Currently showing segment count only

### Express Route Connector Count May Vary
- ER tunnel-to-connector ratio set to 3:1 (confirmed for MSK, may vary)

### Reserved Instance Savings
- Current cost ~$10/month On-Demand
- 1-year Reserved Instance would reduce to ~$5.29/month — saving ~$38/year
- No downtime required — just a billing change in AWS console
