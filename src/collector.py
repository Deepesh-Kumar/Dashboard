#!/usr/bin/env python3
"""Collect metrics from Selector MCP API for all tenants.

Uses the Selector MCP endpoint (SSE transport) to run S2QL queries.
Runs 6 API calls total (1 label query + 5 bulk S2QL queries),
then splits results by tenant_prefix and saves as JSON.
"""

import json
import os
import re
import sys
import time
import uuid
from datetime import datetime

import requests

from config import SELECTOR_API_KEY, DATA_DIR
from cxp_mapping import SIZE_TO_GBPS, size_rank

# MCP endpoint
MCP_BASE_URL = "https://alkira-prod.selector.ai/mcp"


def mcp_initialize_session():
    """Initialize a Streamable HTTP MCP session and return the session ID."""
    headers = {
        "Authorization": f"Bearer {SELECTOR_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "dashboard-collector", "version": "1.0"},
        },
    }
    try:
        resp = requests.post(f"{MCP_BASE_URL}/", json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        session_id = resp.headers.get("mcp-session-id")
        if session_id:
            # Send initialized notification (required before tools/call)
            headers["mcp-session-id"] = session_id
            requests.post(f"{MCP_BASE_URL}/", json={
                "jsonrpc": "2.0", "method": "notifications/initialized",
            }, headers=headers, timeout=10)
            return session_id
        print("  WARNING: No mcp-session-id in response headers")
    except Exception as e:
        print(f"  MCP init failed: {e}")
    return None


def mcp_call_tool(tool_name, arguments, session_id=None, retries=3):
    """Call an MCP tool via Streamable HTTP JSON-RPC POST."""
    headers = {
        "Authorization": f"Bearer {SELECTOR_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["mcp-session-id"] = session_id
    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }

    for attempt in range(retries):
        try:
            resp = requests.post(f"{MCP_BASE_URL}/", json=payload, headers=headers, timeout=600)
            resp.raise_for_status()

            # Response may be SSE format (text/event-stream) or plain JSON
            result = None
            if "text/event-stream" in resp.headers.get("content-type", ""):
                for line in resp.text.strip().split("\n"):
                    if line.startswith("data: "):
                        result = json.loads(line[6:])
                        break
            else:
                result = resp.json()

            if result is None:
                raise ValueError("No data in SSE response")

            # Extract data from JSON-RPC response
            if "result" in result:
                content = result["result"]
                if isinstance(content, dict) and "content" in content:
                    for item in content["content"]:
                        if item.get("type") == "text":
                            try:
                                return json.loads(item["text"])
                            except json.JSONDecodeError:
                                return item["text"]
                return content
            if "error" in result:
                print(f"  MCP error: {result['error']}")
                return None
            return result
        except (requests.RequestException, json.JSONDecodeError) as e:
            wait = 2 ** attempt * 5
            print(f"  Attempt {attempt + 1}/{retries} failed: {e}. Retrying in {wait}s...")
            time.sleep(wait)

    print(f"  ERROR: All {retries} attempts failed for tool: {tool_name}")
    return None


def get_tenant_list(session_id=None):
    """Get list of all tenant prefixes via MCP label values tool."""
    print("  Fetching tenant list...")
    result = mcp_call_tool(
        "query_selector_tsdb_label_values",
        {"params": {"name": "tenant_prefix"}},
        session_id=session_id,
    )
    if result:
        # Result might be {"status": "success", "data": [...]}
        if isinstance(result, dict) and "data" in result:
            return result["data"]
        if isinstance(result, list):
            return result
    print("  WARNING: Could not fetch tenant list from MCP")
    return []


def query_selector(command, session_id=None):
    """Run an S2QL query via the MCP query_selector tool."""
    result = mcp_call_tool(
        "query_selector",
        {"params": {"command": command}},
        session_id=session_id,
    )
    if result:
        if isinstance(result, dict) and "data" in result:
            return result["data"]
        if isinstance(result, list):
            return result
    return []


def promql_query(expr, session_id=None):
    """Run a PromQL instant query via query_selector_tsdb_metric_query."""
    result = mcp_call_tool(
        "query_selector_tsdb_metric_query",
        {"params": {"expr": expr, "query_type": "instant"}},
        session_id=session_id,
    )
    if result and isinstance(result, dict) and "data" in result:
        return result["data"].get("result", [])
    return []


def extract_rows(data):
    """Normalize data into a list of dicts."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if "data" in data and isinstance(data["data"], list):
            return data["data"]
    return []


def collect_all():
    """Run all collection queries and return structured metrics."""
    print(f"[{datetime.now().isoformat()}] Starting collection...")

    # Initialize Streamable HTTP MCP session
    print("  Initializing MCP session...")
    session_id = mcp_initialize_session()
    if session_id:
        print(f"  Session ID: {session_id[:40]}...")
    else:
        print("  WARNING: No session ID, requests may fail")

    # Step 0: Get tenant list
    tenants = get_tenant_list(session_id)
    print(f"  Found {len(tenants)} tenants")

    # Step 1: BW RX per CXP (all tenants)
    print("  Querying BW RX per CXP (all tenants)...")
    bw_rx_rows = query_selector(
        "#select s2_connector_bw_cxp_tenant_con_rx_avg_rollup as table "
        "group-by cxp_name, tenant_prefix in last 30 days",
        session_id=session_id,
    )
    print(f"    Got {len(bw_rx_rows)} BW RX records")

    # Step 2: BW TX per CXP (all tenants)
    print("  Querying BW TX per CXP (all tenants)...")
    bw_tx_rows = query_selector(
        "#select s2_connector_bw_cxp_tenant_con_tx_avg_rollup as table "
        "group-by cxp_name, tenant_prefix in last 30 days",
        session_id=session_id,
    )
    print(f"    Got {len(bw_tx_rows)} BW TX records")

    # Step 3: Connector inventory
    print("  Querying connector inventory (all tenants)...")
    connector_rows = query_selector(
        "#select s2_connector_health as table group-by tag, tenant, cxp_name",
        session_id=session_id,
    )
    print(f"    Got {len(connector_rows)} connector records")

    # Step 4: BW utilization %
    print("  Querying BW utilization (all tenants)...")
    util_rows = query_selector(
        "#select s2_connector_bw_pct as table group-by name, cxp_name, tenant, size, csn_name, type",
        session_id=session_id,
    )
    print(f"    Got {len(util_rows)} utilization records")

    # Step 5: Connector inventory (unique connectors from bw_pct)
    print("  Querying connector inventory from bw_pct (all tenants)...")
    inventory_rows = query_selector(
        "#select s2_connector_bw_pct as table group-by connectorId, connectorName, tenant_prefix, tenant, cxp_name, name, type",
        session_id=session_id,
    )
    print(f"    Got {len(inventory_rows)} connector inventory records")

    # Step 6: Data egress (direct byte counters per tenant per CXP)
    print("  Querying data egress bytes (all tenants)...")
    egress_rows = query_selector(
        "#select aggregate.sum.s2_connector_bw_tenant_tx_bytes:tx_bytes,"
        "aggregate.sum.s2_connector_bw_tenant_rx_bytes:rx_bytes "
        "in s2_connector_bw_tenant_bytes as table "
        "group-by cxp_name, tenant_prefix in last 30 days",
        session_id=session_id,
    )
    print(f"    Got {len(egress_rows)} egress records")

    # Step 6a: Egress breakdown — CON TX bytes (connector traffic)
    print("  Querying CON TX bytes (all tenants)...")
    egress_con_rows = query_selector(
        "#select s2_connector_bw_cxp_tenant_con_tx_bytes as table "
        "group-by cxp_name, tenant_prefix in last 30 days",
        session_id=session_id,
    )
    print(f"    Got {len(egress_con_rows)} CON TX records")

    # Step 6b: Egress breakdown — SVC TX bytes (service traffic)
    print("  Querying SVC TX bytes (all tenants)...")
    egress_svc_rows = query_selector(
        "#select s2_connector_bw_cxp_tenant_svc_tx_bytes as table "
        "group-by cxp_name, tenant_prefix in last 30 days",
        session_id=session_id,
    )
    print(f"    Got {len(egress_svc_rows)} SVC TX records")

    # Step 6c: Egress breakdown — INET TX bytes
    print("  Querying INET TX bytes (all tenants)...")
    egress_inet_rows = query_selector(
        "#select s2_connector_bw_cxp_tenant_inet_tx_bytes as table "
        "group-by cxp_name, tenant_prefix in last 30 days",
        session_id=session_id,
    )
    print(f"    Got {len(egress_inet_rows)} INET TX records")

    # Step 6d: Egress breakdown — Branch TX bytes
    print("  Querying Branch TX bytes (all tenants)...")
    egress_branch_rows = query_selector(
        "#select s2_connector_bw_cxp_tenant_branch_tx_bytes as table "
        "group-by cxp_name, tenant_prefix in last 30 days",
        session_id=session_id,
    )
    print(f"    Got {len(egress_branch_rows)} Branch TX records")

    # Step 6e: Egress breakdown — Cloud TX bytes
    print("  Querying Cloud TX bytes (all tenants)...")
    egress_cloud_rows = query_selector(
        "#select s2_connector_bw_cxp_tenant_cloud_tx_bytes as table "
        "group-by cxp_name, tenant_prefix in last 30 days",
        session_id=session_id,
    )
    print(f"    Got {len(egress_cloud_rows)} Cloud TX records")

    # Step 6f: Egress breakdown — Inter-CXP TX bytes
    print("  Querying Inter-CXP TX bytes (all tenants)...")
    egress_intercxp_rows = query_selector(
        "#select s2_connector_bw_cxp_tenant_intercxp_tx_bytes as table "
        "group-by cxp_name, tenant_prefix in last 30 days",
        session_id=session_id,
    )
    print(f"    Got {len(egress_intercxp_rows)} Inter-CXP TX records")

    # Re-initialize session before aggregate queries — large earlier responses
    # can exhaust the MCP session state, causing aggregate queries to return 0.
    print("  Re-initializing MCP session for aggregate queries...")
    session_id = mcp_initialize_session()
    if session_id:
        print(f"  New session ID: {session_id[:40]}...")
    else:
        print("  WARNING: Could not refresh session, aggregate queries may fail")

    # Step 7: Peak utilization % (aggregate.max over 30 days)
    print("  Querying peak BW utilization (all tenants)...")
    peak_util_rows = query_selector(
        "#select aggregate.max.s2_connector_bw_pct:peak_pct "
        "in s2_connector_bw_pct as table "
        "group-by name, cxp_name, tenant, size in last 30 days",
        session_id=session_id,
    )
    print(f"    Got {len(peak_util_rows)} peak utilization records")

    # Step 8: Avg utilization above 80% threshold (aggregate.count where >80%)
    print("  Querying avg utilization >80% (all tenants)...")
    hit_count_rows = query_selector(
        "#select aggregate.count.s2_connector_bw_pct:hit_count "
        "in s2_connector_bw_pct as table "
        "group-by name, cxp_name, tenant, size "
        "where s2_connector_bw_pct > 80 in last 30 days",
        session_id=session_id,
    )
    print(f"    Got {len(hit_count_rows)} >80% utilization records")

    # Step 9: Peak BW RX per CXP (aggregate.max over hourly time series, last 30 days)
    print("  Querying peak BW RX per CXP (all tenants)...")
    peak_rx_rows = query_selector(
        "#select aggregate.max.s2_connector_bw_cxp_tenant_con_rx "
        "in s2_connector_bw_cxp_tenant_con_rx as line-plot "
        "group-by cxp_name, tenant_prefix in last 30 days",
        session_id=session_id,
    )
    print(f"    Got {len(peak_rx_rows)} peak BW RX records")

    # Step 10: Peak BW TX per CXP
    print("  Querying peak BW TX per CXP (all tenants)...")
    peak_tx_rows = query_selector(
        "#select aggregate.max.s2_connector_bw_cxp_tenant_con_tx "
        "in s2_connector_bw_cxp_tenant_con_tx as line-plot "
        "group-by cxp_name, tenant_prefix in last 30 days",
        session_id=session_id,
    )
    print(f"    Got {len(peak_tx_rows)} peak BW TX records")

    # Re-initialize session before time series queries
    print("  Re-initializing MCP session for time series queries...")
    session_id = mcp_initialize_session()
    if session_id:
        print(f"  New session ID: {session_id[:40]}...")

    # Step 11: Full CXP RX time series — used to count overages above allocated BW
    print("  Querying CXP RX time series (all tenants)...")
    cxp_rx_ts_rows = query_selector(
        "#select s2_connector_bw_cxp_tenant_con_rx as line-plot "
        "group-by cxp_name, tenant_prefix in last 30 days",
        session_id=session_id,
    )
    print(f"    Got {len(cxp_rx_ts_rows)} CXP RX time series records")

    # Build size lookup: {(tenant_prefix, cxp_name): max_size_str}
    # Used to determine the allocated BW threshold per CXP per tenant.
    # util_rows may use tenant (sub-tenant/segment) so strip any 2-digit segment suffix.
    size_lookup = {}
    for row in util_rows:
        tenant = row.get("tenant", "") or row.get("tenant_prefix", "")
        cxp = row.get("cxp_name", "")
        size = row.get("size", "")
        if not (tenant and cxp and size):
            continue
        m = re.match(r'^(.+)-(\d{2})$', tenant)
        prefix = m.group(1) if m else tenant
        key = (prefix, cxp)
        if key not in size_lookup or size_rank(size) > size_rank(size_lookup[key]):
            size_lookup[key] = size

    # Count overage intervals: data points where CXP RX > allocated BW
    overage_counts = {}  # {(tenant_prefix, cxp_name): int}
    for row in cxp_rx_ts_rows:
        prefix = row.get("tenant_prefix", "")
        cxp = row.get("cxp_name", "")
        val = float(row.get("s2_connector_bw_cxp_tenant_con_rx", 0) or 0)
        if not (prefix and cxp and val):
            continue
        size = size_lookup.get((prefix, cxp))
        if size:
            allocated_bps = (SIZE_TO_GBPS.get(size.upper(), 0) or 0) * 1e9
            if allocated_bps > 0 and val > allocated_bps:
                key = (prefix, cxp)
                overage_counts[key] = overage_counts.get(key, 0) + 1

    # Build metrics structure
    metrics = {
        "collected_at": datetime.utcnow().isoformat() + "Z",
        "tenant_count": len(tenants),
        "tenants": {},
    }

    def _empty_tenant():
        return {
            "bw_rx": [], "bw_tx": [], "egress": [], "egress_detail": [],
            "bw_rx_peak": [], "bw_tx_peak": [],
            "connectors": [], "connector_list": [], "utilization": [],
            "peak_utilization": [], "peak_hit_count": [],
            "cxp_rx_overages": [], "cxp_thresh_sizes": [],
            "bw_rx_timeseries": {},
        }

    # Initialize all tenants
    for t in tenants:
        metrics["tenants"][t] = _empty_tenant()

    # Also add tenants found in data but not in the label list
    all_data_tenants = set()
    for row in bw_rx_rows:
        t = row.get("tenant_prefix") or row.get("tenant") or ""
        if t:
            all_data_tenants.add(t)
    for t in all_data_tenants:
        if t not in metrics["tenants"]:
            metrics["tenants"][t] = _empty_tenant()

    # Parse BW RX
    for row in bw_rx_rows:
        tenant = row.get("tenant_prefix") or row.get("tenant") or ""
        if tenant in metrics["tenants"]:
            metrics["tenants"][tenant]["bw_rx"].append({
                "cxp_name": row.get("cxp_name", ""),
                "value": row.get("value", row.get("s2_connector_bw_cxp_tenant_con_rx_avg_rollup", 0)),
            })

    # Parse BW TX
    for row in bw_tx_rows:
        tenant = row.get("tenant_prefix") or row.get("tenant") or ""
        if tenant in metrics["tenants"]:
            metrics["tenants"][tenant]["bw_tx"].append({
                "cxp_name": row.get("cxp_name", ""),
                "value": row.get("value", row.get("s2_connector_bw_cxp_tenant_con_tx_avg_rollup", 0)),
            })

    # Parse peak BW RX (aggregate.max on hourly time series — value is bps directly)
    for row in peak_rx_rows:
        tenant = row.get("tenant_prefix") or row.get("tenant") or ""
        if tenant in metrics["tenants"]:
            metrics["tenants"][tenant]["bw_rx_peak"].append({
                "cxp_name": row.get("cxp_name", ""),
                "value": row.get("s2_connector_bw_cxp_tenant_con_rx", row.get("value", 0)),
            })

    # Parse peak BW TX
    for row in peak_tx_rows:
        tenant = row.get("tenant_prefix") or row.get("tenant") or ""
        if tenant in metrics["tenants"]:
            metrics["tenants"][tenant]["bw_tx_peak"].append({
                "cxp_name": row.get("cxp_name", ""),
                "value": row.get("s2_connector_bw_cxp_tenant_con_tx", row.get("value", 0)),
            })

    # Parse connectors
    for row in connector_rows:
        tenant = row.get("tenant") or row.get("tenant_prefix") or ""
        if tenant not in metrics["tenants"]:
            metrics["tenants"][tenant] = _empty_tenant()
        metrics["tenants"][tenant]["connectors"].append({
            "tag": row.get("tag", ""),
            "cxp_name": row.get("cxp_name", ""),
            "count": row.get("count", row.get("value", 1)),
        })

    # Parse connector inventory (unique connectors from bw_pct)
    for row in inventory_rows:
        tenant = row.get("tenant_prefix") or row.get("tenant") or ""
        if tenant not in metrics["tenants"]:
            metrics["tenants"][tenant] = _empty_tenant()
        metrics["tenants"][tenant]["connector_list"].append({
            "connectorId": row.get("connectorId", ""),
            "connectorName": row.get("connectorName", ""),
            "name": row.get("name", ""),
            "cxp_name": row.get("cxp_name", ""),
            "type": row.get("type", ""),
            "segment": row.get("tenant", ""),
        })

    # Parse egress bytes
    for row in egress_rows:
        tenant = row.get("tenant_prefix") or row.get("tenant") or ""
        if tenant not in metrics["tenants"]:
            metrics["tenants"][tenant] = _empty_tenant()
        agg = row.get("aggregate", {}).get("sum", {})
        metrics["tenants"][tenant]["egress"].append({
            "cxp_name": row.get("cxp_name", ""),
            "tx_bytes": agg.get("s2_connector_bw_tenant_tx_bytes", row.get("tx_bytes", 0)),
            "rx_bytes": agg.get("s2_connector_bw_tenant_rx_bytes", row.get("rx_bytes", 0)),
        })

    # Parse egress breakdown (INET / Branch / Cloud / Inter-CXP)
    # Build a combined lookup keyed by (tenant, cxp_name) then merge into egress_detail list
    _egress_detail = {}  # {(tenant, cxp): {inet_tx, branch_tx, cloud_tx, intercxp_tx}}

    def _parse_egress_type(rows, field_key, display_key):
        for row in rows:
            tenant = row.get("tenant_prefix") or row.get("tenant") or ""
            cxp = row.get("cxp_name", "")
            if not tenant or not cxp:
                continue
            val = float(row.get(field_key, row.get("value", 0)) or 0)
            key = (tenant, cxp)
            _egress_detail.setdefault(key, {})
            # Sum across multiple rows for the same (tenant, cxp) if any
            _egress_detail[key][display_key] = _egress_detail[key].get(display_key, 0) + val

    _parse_egress_type(egress_con_rows,     "s2_connector_bw_cxp_tenant_con_tx_bytes",     "con_tx")
    _parse_egress_type(egress_svc_rows,     "s2_connector_bw_cxp_tenant_svc_tx_bytes",     "svc_tx")
    _parse_egress_type(egress_inet_rows,    "s2_connector_bw_cxp_tenant_inet_tx_bytes",    "inet_tx")
    _parse_egress_type(egress_branch_rows,  "s2_connector_bw_cxp_tenant_branch_tx_bytes",  "branch_tx")
    _parse_egress_type(egress_cloud_rows,   "s2_connector_bw_cxp_tenant_cloud_tx_bytes",   "cloud_tx")
    _parse_egress_type(egress_intercxp_rows,"s2_connector_bw_cxp_tenant_intercxp_tx_bytes","intercxp_tx")

    for (tenant, cxp), breakdown in _egress_detail.items():
        if tenant not in metrics["tenants"]:
            metrics["tenants"][tenant] = _empty_tenant()
        metrics["tenants"][tenant]["egress_detail"].append({
            "cxp_name": cxp,
            **breakdown,
        })

    # Parse utilization
    for row in util_rows:
        tenant = row.get("tenant") or row.get("tenant_prefix") or ""
        if tenant not in metrics["tenants"]:
            metrics["tenants"][tenant] = _empty_tenant()
        metrics["tenants"][tenant]["utilization"].append({
            "name": row.get("name", ""),
            "cxp_name": row.get("cxp_name", ""),
            "size": row.get("size", ""),
            "csn_name": row.get("csn_name", ""),
            "type": row.get("type", ""),
            "value": row.get("value", row.get("s2_connector_bw_pct", 0)),
        })

    # Parse peak utilization (aggregate.max — value field is s2_connector_bw_pct)
    if peak_util_rows:
        for row in peak_util_rows:
            tenant = row.get("tenant") or row.get("tenant_prefix") or ""
            if tenant not in metrics["tenants"]:
                metrics["tenants"][tenant] = _empty_tenant()
            peak_pct = row.get("s2_connector_bw_pct", row.get("peak_pct", 0))
            metrics["tenants"][tenant]["peak_utilization"].append({
                "name": row.get("name", ""),
                "cxp_name": row.get("cxp_name", ""),
                "size": row.get("size", ""),
                "peak_pct": peak_pct,
            })

    # Parse avg-when-high utilization (aggregate.count where >80% — value is avg util during those periods)
    if hit_count_rows:
        for row in hit_count_rows:
            tenant = row.get("tenant") or row.get("tenant_prefix") or ""
            if tenant not in metrics["tenants"]:
                metrics["tenants"][tenant] = _empty_tenant()
            high_pct = row.get("s2_connector_bw_pct", row.get("hit_count", 0))
            metrics["tenants"][tenant]["peak_hit_count"].append({
                "name": row.get("name", ""),
                "cxp_name": row.get("cxp_name", ""),
                "size": row.get("size", ""),
                "hit_count": high_pct,
            })

    # Store overage counts per tenant
    for (prefix, cxp), count in overage_counts.items():
        if prefix in metrics["tenants"]:
            metrics["tenants"][prefix]["cxp_rx_overages"].append({
                "cxp_name": cxp,
                "count": count,
            })

    # Store time series per tenant for chart rendering
    for row in cxp_rx_ts_rows:
        prefix = row.get("tenant_prefix", "")
        cxp = row.get("cxp_name", "")
        ts = row.get("timestamp", 0)
        val = float(row.get("s2_connector_bw_cxp_tenant_con_rx", 0) or 0)
        if prefix in metrics["tenants"] and cxp and ts:
            ts_map = metrics["tenants"][prefix]["bw_rx_timeseries"]
            if cxp not in ts_map:
                ts_map[cxp] = []
            ts_map[cxp].append([ts, val])

    # Step 12: CXP size from cxp_highThresh (PromQL) — independent of active connectors.
    # Gives CXP size for tenants whose connectors are deprovisioned (e.g. Arctera).
    print("  Querying CXP size from cxp_highThresh (PromQL)...")
    thresh_series = promql_query(
        "max by (tenantId, cxp_name, size) (cxp_highThresh)",
        session_id=session_id,
    )
    print(f"    Got {len(thresh_series)} cxp_highThresh series")

    # Build tenantId → tenant_prefix reverse mapping (parent tenants only)
    tenantid_to_prefix = {}
    for prefix in metrics["tenants"]:
        if re.match(r'^[a-z-]{3,8}\d{7}$', prefix):
            tenant_id_str = str(int(prefix[-7:]))
            tenantid_to_prefix[tenant_id_str] = prefix

    for series in thresh_series:
        m = series.get("metric", {})
        tenant_id = m.get("tenantId", "")
        cxp = m.get("cxp_name", "")
        size = m.get("size", "")
        if not (tenant_id and cxp and size):
            continue
        prefix = tenantid_to_prefix.get(tenant_id)
        if not prefix:
            continue
        metrics["tenants"][prefix]["cxp_thresh_sizes"].append({
            "cxp_name": cxp,
            "size": size,
        })
        # Also fill size_lookup so overage calculations benefit
        key = (prefix, cxp)
        if key not in size_lookup or size_rank(size) > size_rank(size_lookup[key]):
            size_lookup[key] = size

    # Flag empty tenants
    empty_tenants = [
        t for t, d in metrics["tenants"].items()
        if not d["bw_rx"] and not d["bw_tx"] and not d["egress"]
        and not d["connectors"] and not d["utilization"]
    ]
    for t in empty_tenants:
        metrics["tenants"][t]["_empty"] = True

    populated = len(metrics["tenants"]) - len(empty_tenants)
    print(f"  {populated} tenants with data, {len(empty_tenants)} empty")

    return metrics


def save_metrics(metrics):
    """Save metrics to a timestamped JSON file.

    Preserves peak BW fields from the previous latest.json when the current
    run returned 0 records for those fields (e.g. Selector query timeout).
    """
    os.makedirs(DATA_DIR, exist_ok=True)

    # Preserve previous peak fields if current run returned none
    latest_path = os.path.join(DATA_DIR, "latest.json")
    # Only preserve overage/hit counts — not bw_rx_peak/bw_tx_peak (those would show stale values in CXP summary)
    peak_fields = ["peak_utilization", "peak_hit_count", "cxp_rx_overages"]
    if os.path.exists(latest_path):
        try:
            with open(latest_path) as f:
                prev = json.load(f)
            prev_tenants = prev.get("tenants", {})
            preserved = 0
            for prefix, data in metrics["tenants"].items():
                prev_data = prev_tenants.get(prefix, {})
                for field in peak_fields:
                    if not data.get(field) and prev_data.get(field):
                        data[field] = prev_data[field]
                        preserved += 1
            if preserved:
                print(f"  Preserved {preserved} peak fields from previous run")
        except Exception as e:
            print(f"  Warning: could not load previous data for peak preservation: {e}")

    filename = f"metrics-{datetime.utcnow().strftime('%Y-%m')}.json"
    filepath = os.path.join(DATA_DIR, filename)
    with open(filepath, "w") as f:
        json.dump(metrics, f, indent=2)
    print(f"  Saved to {filepath}")

    with open(latest_path, "w") as f:
        json.dump(metrics, f, indent=2)
    print(f"  Saved to {latest_path}")

    return filepath


def main():
    metrics = collect_all()
    populated = len(metrics["tenants"]) - len(
        [t for t, d in metrics["tenants"].items() if d.get("_empty")]
    )
    if populated == 0:
        print("ERROR: No tenant data collected. Keeping existing latest.json.")
        return 1
    filepath = save_metrics(metrics)
    print(f"[{datetime.now().isoformat()}] Collection complete: {filepath}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
