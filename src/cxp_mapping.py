"""CXP to cloud provider mapping and connector type detection."""


def cxp_to_cloud(cxp_name):
    """Map a CXP name to its cloud provider."""
    name = cxp_name.upper()
    if "AZURE" in name:
        return "Azure"
    if "GCP" in name:
        return "GCP"
    return "AWS"


def connector_tag_to_type(tag):
    """Map a connector or tunnel tag prefix to a human-readable type."""
    tag = tag.lower() if tag else ""
    mapping = {
        # Connector tags (con-) — each tag is one connector
        # Health tag prefixes
        "con-aws_vpc": "AWS VPC",
        "con-azure_vn": "Azure VNet",
        "con-gcp_vpc": "GCP VPC",
        "con-ipsec_adv": "IPSec Advanced",
        "con-ipsec": "IPSec",
        "con-aws_dc": "Direct Connect",
        "con-azure_er": "Express Route",
        "con-aws_tgw": "AWS TGW",
        "con-internet": "Internet",
        "con-inb_int": "Internet Inbound",
        "con-remote": "Remote Access",
        "con-cisco_sd": "Cisco SD-WAN",
        "con-fortinet": "Fortinet SD-WAN",
        "con-vmware_sd": "VMware SD-WAN",
        "con-versa_sd": "Versa SD-WAN",
        "con-aruba": "Aruba Edge",
        "con-oci_vcn": "OCI VCN",
        "con-gcp_int": "GCP Interconnect",
        "con-saas": "SaaS",
        # bw_pct connectorName prefixes (same types, different naming)
        "con-ip_sec": "IPSec",
        "con-adv_ip_s": "IPSec Advanced",
        "con-direct_c": "Direct Connect",
        "con-express_": "Express Route",
        "con-sd_wan": "Cisco SD-WAN",
        "con-aruba_ed": "Aruba Edge",
        "con-vmware_s": "VMware SD-WAN",
        # Tunnel/VIF/circuit tags (coni-) — multiple per parent connector
        "coni-direct_c": "Direct Connect",
        "coni-express_": "Express Route",
        "coni-adv_ip_s": "IPSec Advanced",
        "coni-ip_sec": "IPSec",
        "coni-fortinet": "Fortinet SD-WAN",
        "coni-sd_wan": "Cisco SD-WAN",
        "coni-versa_sd": "Versa SD-WAN",
        "coni-vmware_s": "VMware SD-WAN",
        "coni-aruba_ed": "Aruba Edge",
    }
    for prefix, label in mapping.items():
        if tag.startswith(prefix):
            return label
    return tag or "Unknown"


def cloud_badge_class(cloud):
    """Return CSS class for cloud provider badge."""
    return {
        "AWS": "badge-aws",
        "Azure": "badge-azure",
        "GCP": "badge-gcp",
    }.get(cloud, "badge-other")


def is_countable_connector(tag):
    """Return True if the tag represents a connector or tunnel (con-* or coni-*)."""
    if not tag:
        return False
    t = tag.lower()
    return t.startswith("con-") or t.startswith("coni-")


def is_connector(tag):
    """Return True for actual connectors (con-*), False for tunnels/VIFs (coni-*).

    coni-* tags represent individual tunnels, VIFs, or circuits that belong
    to a parent connector. Multiple coni-* entries may map to the same
    connector, so they should not be counted individually.
    """
    if not tag:
        return False
    t = tag.lower()
    return t.startswith("con-") and not t.startswith("coni-")


def service_tag_to_name(tag):
    """Map a service tag to a human-readable service name. Returns None for non-service tags."""
    if not tag:
        return None
    t = tag.lower()
    if not t.startswith("svc-"):
        return None
    mapping = {
        "svc-pan": "Palo Alto FW",
        "svc-ftntfw": "Fortinet FW",
        "svc-chkpfw": "Check Point FW",
        "svc-ciscoftd": "Cisco FTDv",
        "svc-zia": "Zscaler",
        "svc-f5": "F5 Load Balancer",
        "svc-infoblox": "Infoblox",
    }
    for prefix, name in mapping.items():
        if t.startswith(prefix):
            return name
    return "Unknown Service"


# Cloud connector types — only these count in the "Cloud" column
CLOUD_TYPES = {"AWS VPC", "Azure VNet", "GCP VPC", "OCI VCN", "AWS TGW"}

# On-prem/dedicated connector types
ONPREM_TYPES = {
    "Direct Connect", "Express Route",
    "IPSec", "IPSec Advanced",
    "Cisco SD-WAN", "Fortinet SD-WAN", "VMware SD-WAN", "Versa SD-WAN",
    "Aruba Edge",
}

# How many coni- tunnel/VIF/circuit entries map to one parent connector.
TUNNEL_RATIO = {
    "Direct Connect": 2,
    "Express Route": 3,
    "IPSec Advanced": 4,
    "IPSec": 2,
    "Cisco SD-WAN": 2,
    "Fortinet SD-WAN": 2,
    "VMware SD-WAN": 2,
    "Versa SD-WAN": 2,
    "Aruba Edge": 2,
}


def is_cloud_type(ctype):
    """Return True if the connector type is a cloud VPC/VNet/VCN."""
    return ctype in CLOUD_TYPES


def is_onprem_type(ctype):
    """Return True if the connector type is on-prem/dedicated."""
    return ctype in ONPREM_TYPES


def tunnels_to_connectors(ctype, tunnel_count):
    """Convert tunnel/VIF count to estimated connector count."""
    ratio = TUNNEL_RATIO.get(ctype, 1)
    return max(1, -(-tunnel_count // ratio))  # ceiling division


def utilization_class(pct):
    """Return CSS class based on utilization percentage."""
    try:
        val = float(pct)
    except (TypeError, ValueError):
        return "util-unknown"
    if val < 50:
        return "util-green"
    if val < 80:
        return "util-yellow"
    return "util-red"


# LARGE = 1 Gbps base unit; NLARGES = N Gbps directly.
# SMALL = 100 Mbps (0.1 Gbps), MEDIUM = 500 Mbps (0.5 Gbps).
SIZE_TO_GBPS = {
    "SMALL": 0.1, "MEDIUM": 0.5, "LARGE": 1,
    "2LARGE": 2, "5LARGE": 5, "10LARGE": 10,
    "20LARGE": 20, "30LARGE": 30,
}

# Ordered list for max-size comparison (smallest to largest)
SIZE_ORDER = ["SMALL", "MEDIUM", "LARGE", "2LARGE", "5LARGE", "10LARGE", "20LARGE", "30LARGE"]


def size_rank(size_str):
    """Return sort rank for a size string (higher = larger)."""
    try:
        return SIZE_ORDER.index((size_str or "").upper())
    except ValueError:
        return -1


def size_to_gbps(size_str):
    """Return allocated Gbps for a T-shirt size string, or None if unknown."""
    return SIZE_TO_GBPS.get((size_str or "").upper())
