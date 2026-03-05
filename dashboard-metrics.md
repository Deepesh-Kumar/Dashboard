# Dashboard Metrics Project

## Architecture
Static HTML dashboard collecting Alkira tenant metrics from Selector AI MCP API → generates static HTML → served via Nginx on `alkira-dashboard.duckdns.org`.
- **Auth**: Cookie-based session auth (`alkira_session` cookie). Login page validates credentials via nginx basic auth on `/auth-check`, sets cookie on success. Nginx `map` checks cookie on all dashboard pages, redirects to `/login.html` if missing.
- **Nginx config**: `/etc/nginx/conf.d/dashboard.conf` (single server block with `map` for cookie auth)

## Dashboard EC2 Server
| Property | Value |
|----------|-------|
| Instance ID | `i-0beda96326bb4fed8` |
| Instance Name | `linux-dashboard` |
| Elastic IP | `54.190.170.4` |
| Region | `us-west-2` (Oregon) |
| Instance Type | `t3.micro` |
| SSH User | `ubuntu` |
| SSH Key | `~/Downloads/aws-keys/deepesh-us-west-2.pem` |
| Remote Dir | `/opt/dashboard` |
| Web Root | `/var/www/dashboard` |
| Domain | `alkira-dashboard.duckdns.org` |
| Basic Auth | `dashboard` / `Alkira2026` |
| Tags | Name=linux-dashboard, ResourceOwner=deepesh@alkira.net, Forever=True, NotDelete=True |
| Termination Protection | Enabled |

## Components
- `src/collector.py` - Fetches data from Selector MCP (Streamable HTTP transport), runs 7 S2QL queries (tenant list, BW RX, BW TX, connector health, BW utilization, connector inventory from bw_pct with segment, data egress bytes)
- `src/generator.py` - Reads `latest.json`, generates index + per-tenant HTML via Jinja2. Merges sub-tenant health data into parent tenants. Filters sub-tenants from index page.
- `src/cxp_mapping.py` - CXP→cloud mapping, connector tag→type mapping (con-* and coni-* and bw_pct prefixes), service tag→name mapping, utilization color coding
- `src/config.py` - Config (Selector API URL/key, paths)
- `templates/` - base.html, index.html (sortable tenant table), tenant.html (detail page with charts)
- `static/` - dashboard.js (search, sorting, Chart.js), style.css
- `deploy.sh` - Rsync deploy to EC2, sets up Nginx + cron
- `nginx/dashboard.conf` - Nginx config (port 80, cookie-based auth with `map` directive)
- `cron/dashboard-metrics` - Weekly cron (Sunday, 2AM UTC)

## Data Files
- `data/latest.json` - Fresh from Selector MCP API (316 tenants, 107 parent tenants on index as of 2026-03-04)
- Selector MCP API (`alkira-prod.selector.ai/mcp`) — working using Streamable HTTP MCP protocol

## Deployment
```bash
# Deploy code + templates + static assets to EC2:
rsync -avz -e "ssh -i ~/Downloads/aws-keys/deepesh-us-west-2.pem" \
  src/ templates/ static/ data/latest.json \
  ubuntu@54.190.170.4:/opt/dashboard/ --relative

# Then regenerate + publish on the server:
ssh -i ~/Downloads/aws-keys/deepesh-us-west-2.pem ubuntu@54.190.170.4 \
  "cd /opt/dashboard/src && /opt/dashboard/venv/bin/python3 generator.py && sudo rsync -a --delete /opt/dashboard/output/ /var/www/dashboard/"

# Local dev (uses local data/ and output/ dirs):
DASHBOARD_LOCAL=1 python3 src/generator.py
```

---

## Completed Changes

### Collector: Streamable HTTP MCP Protocol (2026-02-16)
- Rewrote from old SSE protocol (GET `/mcp/`) to Streamable HTTP MCP protocol (POST `/mcp/`)
- Protocol: POST `initialize` → get `mcp-session-id` header → send `notifications/initialized` → POST `tools/call` with session ID
- Response parsing: handles both SSE (`event: message\ndata: {json}`) and plain JSON
- Tool params must be wrapped: `{"params": {"name": "tenant_prefix"}}` not `{"name": "tenant_prefix"}`
- Safety guard: won't overwrite `latest.json` if 0 populated tenants

### Collector: 7 S2QL Queries
1. Tenant list (`query_selector_tsdb_label_values` for `tenant_prefix`)
2. BW RX per CXP (`s2_connector_bw_cxp_tenant_con_rx_avg_rollup`)
3. BW TX per CXP (`s2_connector_bw_cxp_tenant_con_tx_avg_rollup`)
4. Connector health (`s2_connector_health` group-by tag, tenant, cxp_name)
5. BW utilization (`s2_connector_bw_pct` group-by name, cxp_name, tenant, size, csn_name, type)
6. Connector inventory (`s2_connector_bw_pct` group-by connectorId, connectorName, tenant_prefix, **tenant**, cxp_name, name, type) — `tenant` field gives segment-level ID
7. Data egress bytes (`s2_connector_bw_tenant_bytes` — direct TX/RX byte counters per tenant per CXP, 30 days)

### Collector: Preserve Peak Fields on Empty Runs
- `save_metrics()` loads previous `latest.json` before saving and carries forward non-empty `peak_utilization`, `peak_hit_count`, `cxp_rx_overages` fields when the current run returns 0 records
- `bw_rx_peak` / `bw_tx_peak` are NOT preserved — peak BW is now derived from time series (always available)

### Generator: Sub-Tenant Merging
- `s2_connector_bw_pct` groups by `tenant_prefix` (base tenant), `s2_connector_health` groups by `tenant` (sub-tenant = segment)
- Generator merges sub-tenant health data, utilization data, and connector data into parent tenants
- Each merged entry tagged with `_segment` for segment attribution
- Sub-tenants filtered from index page (316 total → 107 parent tenants shown)

### Connector Counting
- **Source of truth**: `connector_list` from bw_pct (has unique `connectorId`) preferred over health tags
- **Dedup**: health tags (`con-*`) repeat tenant-wide counts per CXP — deduplicate by tag
- **bw_pct name prefixes** (different from health tags): `con-ip_sec`→IPSec, `con-adv_ip_s`→IPSec Advanced, `con-direct_c`→Direct Connect, `con-express_`→Express Route, `con-sd_wan`→Cisco SD-WAN, `con-aruba_ed`→Aruba Edge, `con-vmware_s`→VMware SD-WAN
- **SaaS**: Not in bw_pct. Merged from health tags (`con-saas*`) when using connector_list path
- **Internet Inbound** (`con-inb_int`): Mapped as "Internet Inbound", excluded from connector breakdown
- **Internet** (`con-internet`): Mapped as "Internet", shown in breakdown (but no tenants currently have these)

### Services Count — Instances, Not Types (2026-03-03)
- Tag format: `svc-{type}-{uuid}:{vm_id}:{tenant_id}:{n}` — UUID before first `:` is the service instance identity
- Deduplicate by `tag.split(":")[0]` to count unique service instances (not unique type names)
- Example: MSK with 3 PAN FW instances shows 3, not 1

### Cloud / On-Prem Connector Split
- Index page shows separate **Cloud** and **On-Prem** columns
- **Cloud types**: AWS VPC, Azure VNet, GCP VPC, OCI VCN, AWS TGW
- **On-prem types**: Direct Connect, Express Route, IPSec, IPSec Advanced, Cisco/Fortinet/VMware/Versa SD-WAN, Aruba Edge
- Other types (Internet Inbound, Remote Access, SaaS, GCP Interconnect) excluded from both columns

### Tunnel-to-Connector Ratios (coni-* entries)
| Type | Ratio | Notes |
|------|-------|-------|
| Direct Connect | 2:1 | 2 VIFs per connector |
| Express Route | 3:1 | 3 circuits per connector |
| IPSec Advanced | 4:1 | 4 tunnels per connector |
| IPSec | 2:1 | 2 tunnels per connector |
| SD-WAN (all) | 2:1 | |
| Aruba Edge | 2:1 | |

### Segment Count
- Connector breakdown shows **Total Segments: N** above the table
- Segment count derived from unique `tenant` values in connector_list (bw_pct)
- Segment names not available in Selector — only numeric IDs (e.g., `koch-0000040-02` → segment 02)

### Data Egress Column
- Uses direct byte counters from `s2_connector_bw_tenant_bytes` metric (Step 7 in collector)
- Falls back to BW-derived calculation if direct bytes unavailable

### BW Rollup Fix
- S2QL `as table ... in last 30 days` returns SUM of 30 daily averages, not actual average
- Divide by 30: `actual_avg_bps = rollup_value / 30`
- Helper functions: `rollup_to_avg_bps()`, `rollup_to_bytes()`

### BW Violators — Time-Series Peak vs Allocated (2026-03-04)
- Counts CXPs where **peak RX > 150% of allocated CXP BW**
- Peak derived from `bw_rx_timeseries` (30-day hourly max per CXP) — always available, no separate peak query needed
- Allocated BW uses max-ranked `size` per CXP from utilization data (same as CXP Summary table)
- `bw_rx_peak` dedicated query is no longer used for violator calculation or CXP summary display

### Tenant Name Cleanup (2026-03-03)
- Index page strips numeric suffixes from tenant names using regex: `re.sub(r'-*\d+$', '', s).rstrip('-')`
- Registered as Jinja2 filter `clean_tenant_name`; href still uses full prefix for linking
- Name override dict for truncated names (applied after regex):

| Key | Display Name | Key | Display Name |
|-----|-------------|-----|-------------|
| organ | organon | proba | probably monsters |
| splun | splunk | adapt | adaptive biotech |
| abbot | abbott | vallo | vallourec |
| pensk | penske | veter | veterans united |
| verit | veritas | steps | stepstone |
| tekio | tekion | spglo | spglobal |
| natur | natures sunshine | texas | texas roadhouse |
| viasa | viasat | pennf | penn foster |
| cyber | cyberdefense | soren | sorenson |
| micha | michaels | avail | availity |
| imper | imperva | mckes | mckesson |
| footl | footlocker | osttr | osttra |
| resto | restorepoint | borgw | borgwarner |
| delta | delta dental | arcte | arctera |
| mesir | mesirow | speci | speciality mckesson |
| leonm | leon medical | commv | commvault |
| labco | labcorp | davit | davita |
| veloc | velocitytech | | |

### CXP Summary Table — Size Column (2026-03-04)
- Added **Size** column (e.g. `LARGE (1 Gbps)`) between Cloud and Peak BW RX (30d)
- Removed separate "CXP Size (Allocated BW)" section from tenant detail page
- Size uses max-ranked connector size per CXP from utilization data

### Services Section
- Services from `svc-*` health tags: Palo Alto FW, Fortinet FW, Check Point FW, Cisco FTDv, Zscaler, F5 Load Balancer, Infoblox
- Shows service name and CXPs on tenant detail page

### UI
- Login page at `/login.html` — cookie-based auth via nginx `map` directive
- Logout button clears session cookie
- Weekly cron: `0 2 * * 0` (Sunday 2AM UTC)

---

## Open Issues

### Segment Names Not Available
- Selector TSDB has no segment name data — only `{tenant_prefix}-{nn}` numeric IDs
- Would need per-tenant Alkira portal API access to get segment names
- Currently showing segment count only (not names)

### Express Route Connector Count May Vary
- ER tunnel-to-connector ratio set to 3:1 (confirmed correct for MSK)
- May not be accurate for all tenants (ratio varies by configuration)

### Collector Cannot Run on EC2
- Selector MCP API (`alkira-prod.selector.ai`) unreachable from EC2 — connection timeout
- Collector runs locally; data + generated HTML deployed via rsync
- Need to investigate EC2 outbound firewall/security group rules
