#!/usr/bin/env python3
"""Generate static HTML dashboard from collected JSON metrics."""

import json
import os
import re
import shutil
import sys

from jinja2 import Environment, FileSystemLoader

from config import DATA_DIR, OUTPUT_DIR, WEB_ROOT
from cxp_mapping import (
    cloud_badge_class,
    connector_tag_to_type,
    cxp_to_cloud,
    is_cloud_type,
    is_connector,
    is_countable_connector,
    is_onprem_type,
    service_tag_to_name,
    size_rank,
    size_to_gbps,
    tunnels_to_connectors,
    utilization_class,
)


def format_bps(value):
    """Format bits/sec value to human-readable string."""
    try:
        val = float(value)
    except (TypeError, ValueError):
        return "N/A"
    if val >= 1e9:
        return f"{val / 1e9:.2f} Gbps"
    if val >= 1e6:
        return f"{val / 1e6:.2f} Mbps"
    if val >= 1e3:
        return f"{val / 1e3:.2f} Kbps"
    return f"{val:.2f} bps"


# The S2QL "as table" query with "in last 30 days" returns the SUM of 30
# daily average-bps data points.  Divide by 30 to get the true average bps;
# total bytes = rollup_sum * 86400 / 8  (the 30s cancel out).
ROLLUP_DAYS = 30


def rollup_to_avg_bps(rollup_sum):
    """Convert rollup sum-of-daily-averages to actual average bps."""
    return rollup_sum / ROLLUP_DAYS


def rollup_to_bytes(rollup_sum):
    """Convert rollup sum-of-daily-averages to total bytes over the period."""
    return rollup_sum * 86400 / 8


def format_bytes(value):
    """Format a byte count to human-readable string."""
    try:
        val = float(value)
    except (TypeError, ValueError):
        return "N/A"
    if val >= 1e15:
        return f"{val / 1e15:.2f} PB"
    if val >= 1e12:
        return f"{val / 1e12:.2f} TB"
    if val >= 1e9:
        return f"{val / 1e9:.2f} GB"
    if val >= 1e6:
        return f"{val / 1e6:.2f} MB"
    if val >= 1e3:
        return f"{val / 1e3:.2f} KB"
    return f"{val:.0f} B"


def format_pct(value):
    """Format a percentage value."""
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return "N/A"


def safe_float(value, default=0.0):
    """Safely convert a value to float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def build_tenant_summary(prefix, data):
    """Build a summary dict for a tenant, used on the index page."""
    # Unique CXPs across all metrics
    cxps = set()
    for entry in data.get("bw_rx", []):
        if entry.get("cxp_name"):
            cxps.add(entry["cxp_name"])
    for entry in data.get("bw_tx", []):
        if entry.get("cxp_name"):
            cxps.add(entry["cxp_name"])
    for entry in data.get("connectors", []):
        if entry.get("cxp_name"):
            cxps.add(entry["cxp_name"])

    # Cloud providers and per-cloud CXP counts
    clouds = sorted(set(cxp_to_cloud(c) for c in cxps))
    cloud_cxp_counts = {}
    for c in cxps:
        cloud = cxp_to_cloud(c)
        cloud_cxp_counts[cloud] = cloud_cxp_counts.get(cloud, 0) + 1

    # Total BW (rollup sums → actual avg bps)
    total_rx_rollup = sum(safe_float(e.get("value")) for e in data.get("bw_rx", []))
    total_tx_rollup = sum(safe_float(e.get("value")) for e in data.get("bw_tx", []))
    total_rx = rollup_to_avg_bps(total_rx_rollup)
    total_tx = rollup_to_avg_bps(total_tx_rollup)

    # Data egress: use direct byte counters if available, else derive from BW
    egress_entries = data.get("egress", [])
    if egress_entries:
        total_egress = sum(safe_float(e.get("tx_bytes")) for e in egress_entries)
    else:
        total_egress = rollup_to_bytes(total_tx_rollup)

    # Count cloud and on-prem connectors.
    # Prefer connector_list (from bw_pct, has unique connectorIds) over
    # health tags (which repeat tenant-wide counts per CXP).
    cloud_connectors = 0
    onprem_connectors = 0
    connector_list = data.get("connector_list", [])
    if connector_list:
        seen_ids = set()
        for entry in connector_list:
            cid = entry.get("connectorId", "")
            if not cid or cid in seen_ids:
                continue
            seen_ids.add(cid)
            tag = entry.get("connectorName", "")
            ctype = connector_tag_to_type(tag)
            if is_cloud_type(ctype):
                cloud_connectors += 1
            elif is_onprem_type(ctype):
                onprem_connectors += 1
    else:
        seen_con_tags = set()  # deduplicate con-* across CXPs
        onprem_tunnels = {}  # {(type, cxp): tunnel_count}
        for e in data.get("connectors", []):
            tag = e.get("tag", "")
            if not is_countable_connector(tag):
                continue
            count = int(safe_float(e.get("count", 1)))
            ctype = connector_tag_to_type(tag)
            cxp = e.get("cxp_name", "Unknown")
            if is_connector(tag):
                if tag in seen_con_tags:
                    continue
                seen_con_tags.add(tag)
                if is_cloud_type(ctype):
                    cloud_connectors += count
                elif is_onprem_type(ctype):
                    onprem_connectors += count
            else:
                key = (ctype, cxp)
                onprem_tunnels[key] = onprem_tunnels.get(key, 0) + count
        for (ctype, _cxp), tunnel_count in onprem_tunnels.items():
            onprem_connectors += tunnels_to_connectors(ctype, tunnel_count)

    # Service count — total service instances
    # Tag format: svc-{type}-{uuid}:{vm_id}:{tenant_id}:{n} — deduplicate by base (before first colon)
    seen_svc_tags = set()
    service_count = 0
    for entry in data.get("connectors", []):
        tag = entry.get("tag", "")
        if service_tag_to_name(tag):
            tag_base = tag.split(":")[0]
            if tag_base not in seen_svc_tags:
                seen_svc_tags.add(tag_base)
                service_count += 1

    # Violator count — CXPs where peak RX (from time series max) > 150% of allocated CXP BW
    peak_rx_by_cxp = {}
    for cxp, pts in data.get("bw_rx_timeseries", {}).items():
        if pts:
            peak_rx_by_cxp[cxp] = max(p[1] for p in pts if len(p) > 1)

    cxp_sizes = {}
    for entry in data.get("utilization", []):
        cxp = entry.get("cxp_name", "")
        size = entry.get("size", "")
        if cxp and size:
            if cxp not in cxp_sizes or size_rank(size) > size_rank(cxp_sizes[cxp]):
                cxp_sizes[cxp] = size

    violator_count = 0
    for cxp, peak_bps in peak_rx_by_cxp.items():
        allocated_bps = (size_to_gbps(cxp_sizes.get(cxp, "")) or 0) * 1e9
        if allocated_bps > 0 and peak_bps > 1.5 * allocated_bps:
            violator_count += 1

    # Top utilization
    util_values = [safe_float(e.get("value")) for e in data.get("utilization", [])]
    top_util = max(util_values) if util_values else 0

    return {
        "prefix": prefix,
        "cxps": sorted(cxps),
        "cxp_count": len(cxps),
        "clouds": clouds,
        "cloud_cxp_counts": cloud_cxp_counts,
        "total_rx": total_rx,
        "total_rx_fmt": format_bps(total_rx),
        "total_tx": total_tx,
        "total_tx_fmt": format_bps(total_tx),
        "total_egress": total_egress,
        "total_egress_fmt": format_bytes(total_egress),
        "cloud_connectors": cloud_connectors,
        "onprem_connectors": onprem_connectors,
        "service_count": service_count,
        "violator_count": violator_count,
        "top_util": top_util,
        "top_util_fmt": format_pct(top_util),
        "top_util_class": utilization_class(top_util),
        "is_empty": data.get("_empty", False),
    }


def build_tenant_detail(prefix, data):
    """Build detailed data for a tenant detail page."""
    # CXP summary table
    cxp_data = {}
    for entry in data.get("bw_rx", []):
        cxp = entry.get("cxp_name", "Unknown")
        if cxp not in cxp_data:
            cxp_data[cxp] = {"cxp_name": cxp, "cloud": cxp_to_cloud(cxp), "bw_rx": 0, "bw_tx": 0}
        cxp_data[cxp]["bw_rx"] += safe_float(entry.get("value"))

    for entry in data.get("bw_tx", []):
        cxp = entry.get("cxp_name", "Unknown")
        if cxp not in cxp_data:
            cxp_data[cxp] = {"cxp_name": cxp, "cloud": cxp_to_cloud(cxp), "bw_rx": 0, "bw_tx": 0}
        cxp_data[cxp]["bw_tx"] += safe_float(entry.get("value"))

    # Build per-CXP egress lookup from direct byte counters
    egress_by_cxp = {}
    for entry in data.get("egress", []):
        cxp = entry.get("cxp_name", "Unknown")
        egress_by_cxp[cxp] = egress_by_cxp.get(cxp, 0) + safe_float(entry.get("tx_bytes"))

    # Build per-CXP egress breakdown (INET / Branch / Cloud / Inter-CXP)
    egress_detail_by_cxp = {}  # {cxp: {con_tx, svc_tx, inet_tx, branch_tx, cloud_tx, intercxp_tx}}
    for entry in data.get("egress_detail", []):
        cxp = entry.get("cxp_name", "Unknown")
        egress_detail_by_cxp[cxp] = {
            "con_tx":      safe_float(entry.get("con_tx")),
            "svc_tx":      safe_float(entry.get("svc_tx")),
            "inet_tx":     safe_float(entry.get("inet_tx")),
            "branch_tx":   safe_float(entry.get("branch_tx")),
            "cloud_tx":    safe_float(entry.get("cloud_tx")),
            "intercxp_tx": safe_float(entry.get("intercxp_tx")),
        }

    # Build per-CXP peak BW from time series (max of 30-day hourly data)
    peak_rx_by_cxp = {}
    for cxp, pts in data.get("bw_rx_timeseries", {}).items():
        if pts:
            peak_rx_by_cxp[cxp] = max(p[1] for p in pts if len(p) > 1)
    peak_tx_by_cxp = {}  # TX time series not collected; leave empty

    # Per-CXP overage count (hourly samples where RX exceeded allocated BW)
    rx_overage_by_cxp = {e["cxp_name"]: e["count"] for e in data.get("cxp_rx_overages", [])}

    cxp_summary = sorted(cxp_data.values(), key=lambda x: x["bw_rx"], reverse=True)
    for c in cxp_summary:
        # Egress: use direct byte counters if available, else derive from BW rollup sum
        if egress_by_cxp:
            c["egress"] = egress_by_cxp.get(c["cxp_name"], 0)
        else:
            c["egress"] = rollup_to_bytes(c["bw_tx"])
        c["egress_fmt"] = format_bytes(c["egress"])
        # Egress breakdown (INET / Branch / Cloud / Inter-CXP)
        bd = egress_detail_by_cxp.get(c["cxp_name"], {})
        c["egress_detail"] = {
            "con_tx":      bd.get("con_tx", 0),
            "svc_tx":      bd.get("svc_tx", 0),
            "inet_tx":     bd.get("inet_tx", 0),
            "branch_tx":   bd.get("branch_tx", 0),
            "cloud_tx":    bd.get("cloud_tx", 0),
            "intercxp_tx": bd.get("intercxp_tx", 0),
            "con_tx_fmt":      format_bytes(bd.get("con_tx", 0)),
            "svc_tx_fmt":      format_bytes(bd.get("svc_tx", 0)),
            "inet_tx_fmt":     format_bytes(bd.get("inet_tx", 0)),
            "branch_tx_fmt":   format_bytes(bd.get("branch_tx", 0)),
            "cloud_tx_fmt":    format_bytes(bd.get("cloud_tx", 0)),
            "intercxp_tx_fmt": format_bytes(bd.get("intercxp_tx", 0)),
            "has_data": bool(bd),
        }
        c["anchor_id"] = "egress-" + c["cxp_name"].replace(" ", "-").lower()
        # Average BW
        c["bw_rx"] = rollup_to_avg_bps(c["bw_rx"])
        c["bw_tx"] = rollup_to_avg_bps(c["bw_tx"])
        c["bw_rx_fmt"] = format_bps(c["bw_rx"])
        c["bw_tx_fmt"] = format_bps(c["bw_tx"])
        # Peak BW (from hourly time series aggregate.max)
        peak_rx = peak_rx_by_cxp.get(c["cxp_name"], 0)
        peak_tx = peak_tx_by_cxp.get(c["cxp_name"], 0)
        c["peak_rx_fmt"] = format_bps(peak_rx) if peak_rx else "N/A"
        c["peak_tx_fmt"] = format_bps(peak_tx) if peak_tx else "N/A"
        # Overage count: number of hourly intervals where RX exceeded allocated BW
        c["rx_overage_count"] = rx_overage_by_cxp.get(c["cxp_name"], 0)
        c["badge_class"] = cloud_badge_class(c["cloud"])

    # Chart data (30-day average bps per CXP)
    chart_labels = [c["cxp_name"] for c in cxp_summary]
    chart_rx = [c["bw_rx"] for c in cxp_summary]
    chart_tx = [c["bw_tx"] for c in cxp_summary]

    # Connector breakdown by type.
    connector_types = {}
    cloud_connectors = 0
    onprem_connectors = 0
    all_segments = set()  # track unique segments across all connectors
    connector_list = data.get("connector_list", [])
    if connector_list:
        # Collect all unique segments first (before connectorId dedup)
        for entry in connector_list:
            seg_full = entry.get("segment", "")
            if seg_full:
                all_segments.add(seg_full)
        seen_ids = set()
        for entry in connector_list:
            cid = entry.get("connectorId", "")
            if not cid or cid in seen_ids:
                continue
            seen_ids.add(cid)
            tag = entry.get("connectorName", "")
            cxp = entry.get("cxp_name", "Unknown")
            ctype = connector_tag_to_type(tag)
            if ctype not in connector_types:
                connector_types[ctype] = {"type": ctype, "count": 0, "cxps": set()}
            connector_types[ctype]["count"] += 1
            connector_types[ctype]["cxps"].add(cxp)
            if is_cloud_type(ctype):
                cloud_connectors += 1
            elif is_onprem_type(ctype):
                onprem_connectors += 1
        # Merge SaaS connectors from health tags (not in bw_pct)
        seen_saas_tags = set()
        for entry in data.get("connectors", []):
            tag = entry.get("tag", "")
            if not tag.lower().startswith("con-saas"):
                continue
            if tag in seen_saas_tags:
                continue
            seen_saas_tags.add(tag)
            cxp = entry.get("cxp_name", "Unknown")
            if "SaaS" not in connector_types:
                connector_types["SaaS"] = {"type": "SaaS", "count": 0, "cxps": set()}
            connector_types["SaaS"]["count"] += 1
            connector_types["SaaS"]["cxps"].add(cxp)
    else:
        seen_con_tags = set()
        onprem_tunnel_groups = {}
        for entry in data.get("connectors", []):
            tag = entry.get("tag", "")
            if not is_countable_connector(tag):
                continue
            count = int(safe_float(entry.get("count", 1)))
            cxp = entry.get("cxp_name", "Unknown")
            ctype = connector_tag_to_type(tag)
            if is_connector(tag):
                if ctype not in connector_types:
                    connector_types[ctype] = {"type": ctype, "count": 0, "cxps": set()}
                connector_types[ctype]["cxps"].add(cxp)
                if tag not in seen_con_tags:
                    seen_con_tags.add(tag)
                    connector_types[ctype]["count"] += count
                    if is_cloud_type(ctype):
                        cloud_connectors += count
                    elif is_onprem_type(ctype):
                        onprem_connectors += count
            else:
                key = (ctype, cxp)
                onprem_tunnel_groups[key] = onprem_tunnel_groups.get(key, 0) + count
                if ctype not in connector_types:
                    connector_types[ctype] = {"type": ctype, "count": 0, "cxps": set()}
                connector_types[ctype]["cxps"].add(cxp)
        for (ctype, _cxp), tunnel_count in onprem_tunnel_groups.items():
            conn_count = tunnels_to_connectors(ctype, tunnel_count)
            connector_types[ctype]["count"] += conn_count
            onprem_connectors += conn_count

    connector_breakdown = sorted(
        [v for v in connector_types.values() if v["type"] != "Internet Inbound"],
        key=lambda x: x["count"], reverse=True,
    )
    for c in connector_breakdown:
        c["cxps"] = sorted(c["cxps"])

    # Per-CXP connector breakdown
    cxp_connector_types = {}  # {cxp_name: {type: count}}
    if connector_list:
        seen_ids_cxp = set()
        for entry in connector_list:
            cid = entry.get("connectorId", "")
            if not cid or cid in seen_ids_cxp:
                continue
            seen_ids_cxp.add(cid)
            tag = entry.get("connectorName", "")
            cxp = entry.get("cxp_name", "Unknown")
            ctype = connector_tag_to_type(tag)
            if ctype == "Internet Inbound":
                continue
            cxp_connector_types.setdefault(cxp, {})[ctype] = cxp_connector_types.get(cxp, {}).get(ctype, 0) + 1
        # SaaS from health tags (not in bw_pct)
        seen_saas_cxp = set()
        for entry in data.get("connectors", []):
            tag = entry.get("tag", "")
            if not tag.lower().startswith("con-saas"):
                continue
            cxp = entry.get("cxp_name", "Unknown")
            key = (tag, cxp)
            if key in seen_saas_cxp:
                continue
            seen_saas_cxp.add(key)
            cxp_connector_types.setdefault(cxp, {})["SaaS"] = cxp_connector_types.get(cxp, {}).get("SaaS", 0) + 1
    else:
        # Health-tag fallback: group by (cxp, type); apply tunnel ratio for coni-* entries
        seen_con_tags_cxp = set()
        cxp_tunnel_groups = {}  # {(cxp, type): tunnel_count}
        for entry in data.get("connectors", []):
            tag = entry.get("tag", "")
            if not is_countable_connector(tag):
                continue
            count = int(safe_float(entry.get("count", 1)))
            cxp = entry.get("cxp_name", "Unknown")
            ctype = connector_tag_to_type(tag)
            if is_connector(tag):
                if tag not in seen_con_tags_cxp:
                    seen_con_tags_cxp.add(tag)
                    cxp_connector_types.setdefault(cxp, {})[ctype] = cxp_connector_types.get(cxp, {}).get(ctype, 0) + count
            else:
                cxp_tunnel_groups[(cxp, ctype)] = cxp_tunnel_groups.get((cxp, ctype), 0) + count
        for (cxp, ctype), tunnel_count in cxp_tunnel_groups.items():
            conn_count = tunnels_to_connectors(ctype, tunnel_count)
            cxp_connector_types.setdefault(cxp, {})[ctype] = cxp_connector_types.get(cxp, {}).get(ctype, 0) + conn_count

    # Per-CXP services
    cxp_services = {}  # {cxp_name: {svc_name: count}}
    seen_svc_by_cxp = {}  # {cxp_name: set of tag_base}
    for entry in data.get("connectors", []):
        tag = entry.get("tag", "")
        svc_name = service_tag_to_name(tag)
        if not svc_name:
            continue
        cxp = entry.get("cxp_name", "Unknown")
        tag_base = tag.split(":")[0]
        seen_svc_by_cxp.setdefault(cxp, set())
        if tag_base in seen_svc_by_cxp[cxp]:
            continue
        seen_svc_by_cxp[cxp].add(tag_base)
        cxp_services.setdefault(cxp, {})[svc_name] = cxp_services.get(cxp, {}).get(svc_name, 0) + 1

    # Services: extract unique service names and their CXPs from svc-* health tags
    services_map = {}
    for entry in data.get("connectors", []):
        tag = entry.get("tag", "")
        svc_name = service_tag_to_name(tag)
        if svc_name:
            cxp = entry.get("cxp_name", "Unknown")
            if svc_name not in services_map:
                services_map[svc_name] = {"name": svc_name, "cxps": set()}
            services_map[svc_name]["cxps"].add(cxp)
    services_list = sorted(services_map.values(), key=lambda x: x["name"])
    for s in services_list:
        s["cxps"] = sorted(s["cxps"])

    # CXP size breakdown — use MAX connector size per CXP.
    # The max size = the highest-capacity CSN node at that CXP.
    cxp_sizes = {}
    for entry in data.get("utilization", []):
        cxp = entry.get("cxp_name", "")
        size = entry.get("size", "")
        if cxp and size:
            if cxp not in cxp_sizes or size_rank(size) > size_rank(cxp_sizes[cxp]):
                cxp_sizes[cxp] = size
    def format_bw(gbps):
        """Format a Gbps value, using Mbps for sub-1G values."""
        if gbps is None:
            return None
        if gbps < 1:
            return f"{int(gbps * 1000)} Mbps"
        return f"{int(gbps)} Gbps"

    # Attach size to cxp_summary entries (used in the Size column)
    for c in cxp_summary:
        size = cxp_sizes.get(c["cxp_name"], "")
        c["size"] = f"{size} ({format_bw(size_to_gbps(size))})" if size and size_to_gbps(size) else size

    # Connector type columns: sorted by total count descending
    connector_type_totals = {}
    for cxp_types in cxp_connector_types.values():
        for ctype, cnt in cxp_types.items():
            connector_type_totals[ctype] = connector_type_totals.get(ctype, 0) + cnt
    connector_type_cols = sorted(connector_type_totals.keys(), key=lambda t: -connector_type_totals[t])

    # Attach per-CXP connector and service breakdown to each CXP summary row
    for c in cxp_summary:
        cxp = c["cxp_name"]
        c["connectors_by_type"] = cxp_connector_types.get(cxp, {})
        c["services_list"] = sorted(
            [{"name": sn, "count": n} for sn, n in cxp_services.get(cxp, {}).items()],
            key=lambda x: x["name"],
        )

    # CXP BW time series for the RX-over-time chart.
    # Points are sorted by timestamp; include the allocated BW threshold per CXP.
    cxp_ts_data = {}
    for cxp, points in data.get("bw_rx_timeseries", {}).items():
        sorted_pts = sorted(points, key=lambda x: x[0])
        size = cxp_sizes.get(cxp)
        allocated_bps = (size_to_gbps(size) or 0) * 1e9 if size else 0
        cxp_ts_data[cxp] = {
            "points": sorted_pts,
            "allocated_bps": allocated_bps,
        }

    # Top connectors by utilization — enrich with peak BW and hit count
    peak_lookup = {
        (r["name"], r["cxp_name"]): safe_float(r.get("peak_pct", 0))
        for r in data.get("peak_utilization", [])
    }
    # hit_count field stores avg utilization % during periods >80%; presence = exceeded 80%
    hit_lookup = {
        (r["name"], r["cxp_name"]): safe_float(r.get("hit_count", 0))
        for r in data.get("peak_hit_count", [])
    }

    utilization = []
    for entry in data.get("utilization", []):
        key = (entry.get("name", ""), entry.get("cxp_name", ""))
        peak_pct = peak_lookup.get(key, 0)
        gbps = size_to_gbps(entry.get("size", "")) or 0
        peak_bw = (peak_pct / 100.0) * gbps if gbps and peak_pct else None
        if peak_bw is not None:
            peak_bw_fmt = format_bw(peak_bw) if peak_bw >= 0.001 else "N/A"
        else:
            peak_bw_fmt = "N/A"
        high_util = hit_lookup.get(key)  # None if never >80%, else avg util when >80%
        utilization.append({
            "name": entry.get("name", ""),
            "cxp_name": entry.get("cxp_name", ""),
            "size": entry.get("size", ""),
            "type": entry.get("type", ""),
            "value": safe_float(entry.get("value")),
            "value_fmt": format_pct(entry.get("value")),
            "util_class": utilization_class(entry.get("value")),
            "peak_pct": peak_pct,
            "peak_bw_fmt": peak_bw_fmt,
            "hit_count": high_util,
        })
    utilization.sort(key=lambda x: x["value"], reverse=True)
    top_utilization = utilization[:50]  # Show top 50

    return {
        "prefix": prefix,
        "cxp_summary": cxp_summary,
        "chart_labels": json.dumps(chart_labels),
        "chart_rx": json.dumps(chart_rx),
        "chart_tx": json.dumps(chart_tx),
        "cxp_ts_json": json.dumps(cxp_ts_data),
        "has_cxp_ts": bool(cxp_ts_data),
        "connector_breakdown": connector_breakdown,
        "connector_type_cols": connector_type_cols,
        "connector_type_totals": connector_type_totals,
        "services_total": sum(s["count"] for c in cxp_summary for s in c.get("services_list", [])),
        "egress_total_fmt": format_bytes(sum(c.get("egress", 0) for c in cxp_summary)),
        "egress_breakdown_totals": {
            "svc_tx":      format_bytes(sum(c["egress_detail"]["svc_tx"]      for c in cxp_summary if c.get("egress_detail", {}).get("has_data"))),
            "intercxp_tx": format_bytes(sum(c["egress_detail"]["intercxp_tx"] for c in cxp_summary if c.get("egress_detail", {}).get("has_data"))),
            "inet_tx":     format_bytes(sum(c["egress_detail"]["inet_tx"]     for c in cxp_summary if c.get("egress_detail", {}).get("has_data"))),
            "branch_tx":   format_bytes(sum(c["egress_detail"]["branch_tx"]   for c in cxp_summary if c.get("egress_detail", {}).get("has_data"))),
            "cloud_tx":    format_bytes(sum(c["egress_detail"]["cloud_tx"]    for c in cxp_summary if c.get("egress_detail", {}).get("has_data"))),
            "total":       format_bytes(sum(c.get("egress", 0)                for c in cxp_summary)),
        },
        "has_egress_detail": bool(egress_detail_by_cxp),
        "services": services_list,
        "segment_count": len(all_segments),
        "top_utilization": top_utilization,
        "cloud_connectors": cloud_connectors,
        "onprem_connectors": onprem_connectors,
    }


def generate():
    """Generate all HTML files from the latest metrics JSON."""
    # Load metrics
    latest_path = os.path.join(DATA_DIR, "latest.json")
    if not os.path.exists(latest_path):
        print(f"ERROR: No metrics file found at {latest_path}")
        print("Run collector.py first.")
        return 1

    with open(latest_path) as f:
        metrics = json.load(f)

    collected_at = metrics.get("collected_at", "Unknown")
    tenants_data = metrics.get("tenants", {})
    print(f"Loaded metrics from {collected_at} with {len(tenants_data)} tenants")

    # Merge sub-tenant health data into parent tenants.
    # bw_pct groups by tenant_prefix (base), health groups by tenant (sub-tenant).
    # Parent tenants with connector_list but no health entries need health data
    # from their sub-tenants for services (svc-*), SaaS (con-saas*), etc.
    all_prefixes = sorted(tenants_data.keys())
    for prefix in all_prefixes:
        data = tenants_data[prefix]
        if data.get("connector_list") and not data.get("connectors"):
            # Find sub-tenants: keys that start with prefix + "-"
            merged = []
            seen_tags = set()
            for sub_prefix in all_prefixes:
                if sub_prefix != prefix and sub_prefix.startswith(prefix + "-"):
                    # Extract segment number from suffix (e.g., "koch-0000040-02" → "02")
                    seg = sub_prefix[len(prefix) + 1:]
                    for entry in tenants_data[sub_prefix].get("connectors", []):
                        tag = entry.get("tag", "")
                        if tag not in seen_tags:
                            seen_tags.add(tag)
                            entry_copy = dict(entry)
                            entry_copy["_segment"] = seg
                            merged.append(entry_copy)
            if merged:
                data["connectors"] = merged
    print(f"  Merged sub-tenant health data into parent tenants")

    # Merge sub-tenant utilization/peak data into parent tenants.
    # utilization, peak_utilization, peak_hit_count are stored under sub-tenants
    # (keyed by tenant = sub-tenant ID) but the index page links to parent pages.
    for prefix in all_prefixes:
        data = tenants_data[prefix]
        if not data.get("utilization"):
            merged_util = []
            merged_peak = []
            merged_hit = []
            for sub_prefix in all_prefixes:
                if sub_prefix != prefix and sub_prefix.startswith(prefix + "-"):
                    merged_util.extend(tenants_data[sub_prefix].get("utilization", []))
                    merged_peak.extend(tenants_data[sub_prefix].get("peak_utilization", []))
                    merged_hit.extend(tenants_data[sub_prefix].get("peak_hit_count", []))
            if merged_util:
                data["utilization"] = merged_util
                data["peak_utilization"] = merged_peak
                data["peak_hit_count"] = merged_hit
    print(f"  Merged sub-tenant utilization data into parent tenants")

    # Set up Jinja2
    template_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")
    env = Environment(loader=FileSystemLoader(template_dir), autoescape=True)
    env.filters["format_bps"] = format_bps
    env.filters["format_pct"] = format_pct
    _NAME_OVERRIDES = {
        "organ": "organon",
        "splun": "splunk",
        "abbot": "abbott",
        "pensk": "penske",
        "verit": "veritas",
        "tekio": "tekion",
        "natur": "natures sunshine",
        "proba": "probably monsters",
        "adapt": "adaptive biotech",
        "vallo": "vallourec",
        "veter": "veterans united",
        "steps": "stepstone",
        "spglo": "spglobal",
        "texas": "texas roadhouse",
        "pennf": "penn foster",
        "soren": "sorenson",
        "avail": "availity",
        "mckes": "mckesson",
        "viasa": "viasat",
        "cyber": "cyberdefense",
        "micha": "michaels",
        "imper": "imperva",
        "footl": "footlocker",
        "resto": "restorepoint",
        "delta": "delta dental",
        "mesir": "mesirow",
        "leonm": "leon medical",
        "commv": "commvault",
        "labco": "labcorp",
        "davit": "davita",
        "veloc": "velocitytech",
        "osttr": "osttra",
        "borgw": "borgwarner",
        "arcte": "arctera",
        "speci": "speciality mckesson",
    }
    def _clean_tenant_name(s):
        base = re.sub(r'-*\d+$', '', s).rstrip('-')
        return _NAME_OVERRIDES.get(base, base)
    env.filters["clean_tenant_name"] = _clean_tenant_name
    env.globals["cloud_badge_class"] = cloud_badge_class
    env.globals["utilization_class"] = utilization_class

    _DIVISION_BADGE = {
        "Sales East":          "badge-east",
        "Sales West":          "badge-west",
        "Sales International": "badge-intl",
        "Sales MidMarket":     "badge-midmkt",
    }
    _DIVISION_SHORT = {
        "Sales East":          "East",
        "Sales West":          "West",
        "Sales International": "International",
        "Sales MidMarket":     "MidMarket",
    }
    env.filters["division_badge_class"] = lambda d: _DIVISION_BADGE.get(d, "badge-other")
    env.filters["division_short"] = lambda d: _DIVISION_SHORT.get(d, d)

    # Prepare output directory
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    tenant_dir = os.path.join(OUTPUT_DIR, "tenant")
    os.makedirs(tenant_dir, exist_ok=True)

    # Identify sub-tenants (segments) — exclude from index page.
    # Sub-tenants have a parent prefix: e.g., "chart0000116-01" is a child of "chart0000116".
    all_keys = set(tenants_data.keys())
    sub_tenants = set()
    for k in all_keys:
        for k2 in all_keys:
            if k != k2 and k.startswith(k2 + "-"):
                sub_tenants.add(k)
                break

    # Build summaries for index page (parent/standalone tenants only)
    summaries = []
    for prefix, data in sorted(tenants_data.items()):
        if prefix in sub_tenants:
            continue
        summaries.append(build_tenant_summary(prefix, data))

    # Sort by total_rx descending (most traffic first)
    summaries.sort(key=lambda x: x["total_rx"], reverse=True)

    totals = {
        "cxp_count":         sum(s["cxp_count"] for s in summaries),
        "cloud_connectors":  sum(s["cloud_connectors"] for s in summaries),
        "onprem_connectors": sum(s["onprem_connectors"] for s in summaries),
        "service_count":     sum(s["service_count"] for s in summaries),
        "violator_count":    sum(s["violator_count"] for s in summaries),
    }

    active_count = len([s for s in summaries if not s["is_empty"]])

    # Load account count for home page card
    accounts_path = os.path.join(DATA_DIR, "accounts.json")
    account_count = 0
    if os.path.exists(accounts_path):
        with open(accounts_path) as f:
            account_count = len(json.load(f).get("accounts", []))

    # Generate home page (portal landing page)
    home_template = env.get_template("home.html")
    home_html = home_template.render(
        collected_at=collected_at,
        active_count=active_count,
        totals=totals,
        account_count=account_count,
    )
    with open(os.path.join(OUTPUT_DIR, "home.html"), "w") as f:
        f.write(home_html)
    print("  Generated home.html")

    # Generate index page (tenant list)
    index_template = env.get_template("index.html")
    index_html = index_template.render(
        tenants=summaries,
        collected_at=collected_at,
        tenant_count=len(summaries),
        active_count=active_count,
        totals=totals,
    )
    with open(os.path.join(OUTPUT_DIR, "index.html"), "w") as f:
        f.write(index_html)
    print(f"  Generated index.html ({len(summaries)} tenants)")

    # Generate login choice page (Okta primary, local as fallback)
    choice_template = env.get_template("login-choice.html")
    choice_html = choice_template.render()
    with open(os.path.join(OUTPUT_DIR, "login-choice.html"), "w") as f:
        f.write(choice_html)
    print("  Generated login-choice.html")

    # Generate login page (EC2 cookie-based)
    login_template = env.get_template("login.html")
    login_html = login_template.render()
    with open(os.path.join(OUTPUT_DIR, "login.html"), "w") as f:
        f.write(login_html)
    print("  Generated login.html")

    # Generate account mapping page
    accounts_path = os.path.join(DATA_DIR, "accounts.json")
    if os.path.exists(accounts_path):
        with open(accounts_path) as f:
            accounts_data = json.load(f)
        accounts = accounts_data.get("accounts", [])
        divisions = sorted(set(a["division"] for a in accounts if a["division"]))
        acct_template = env.get_template("account-mapping.html")
        acct_html = acct_template.render(
            accounts=accounts,
            divisions=divisions,
            source_date=accounts_data.get("source_date", ""),
            collected_at=collected_at,
        )
        with open(os.path.join(OUTPUT_DIR, "account-mapping.html"), "w") as f:
            f.write(acct_html)
        print(f"  Generated account-mapping.html ({len(accounts)} accounts)")
    else:
        print("  Skipping account-mapping.html (no accounts.json)")

    # Generate per-tenant detail pages
    for prefix, data in tenants_data.items():
        detail = build_tenant_detail(prefix, data)
        tenant_template = env.get_template("tenant.html")
        tenant_html = tenant_template.render(
            tenant=detail,
            collected_at=collected_at,
        )
        safe_prefix = prefix.replace("/", "_").replace(" ", "_")
        with open(os.path.join(tenant_dir, f"{safe_prefix}.html"), "w") as f:
            f.write(tenant_html)

    print(f"  Generated {len(tenants_data)} tenant detail pages")

    # Copy static assets
    static_src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
    static_dst = os.path.join(OUTPUT_DIR, "static")
    if os.path.exists(static_src):
        if os.path.exists(static_dst):
            shutil.rmtree(static_dst)
        shutil.copytree(static_src, static_dst)
        print("  Copied static assets")

    print(f"Output written to {OUTPUT_DIR}")
    return 0


def main():
    return generate()


if __name__ == "__main__":
    sys.exit(main())
