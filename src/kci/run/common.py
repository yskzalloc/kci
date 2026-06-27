"""Shared helpers for run targets."""

from __future__ import annotations

import re
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent / "scripts"

KSELFTEST_SETUP = (
    "mount -t tmpfs tmpfs /run; "
    "mkdir -p /run/netns; "
    "ip link set lo up; "
    "ip link set eth0 up 2>/dev/null; "
    "modprobe -a veth bridge tun dummy vxlan bonding team macsec "
    "ipvlan macvlan geneve bareudp amt nf_conntrack nf_nat "
    "ip_tables ip6_tables xt_mark 2>/dev/null; "
    "sysctl -w net.ipv4.conf.all.rp_filter=0 2>/dev/null; "
    "sysctl -w net.ipv4.ping_group_range='0 2147483647' 2>/dev/null"
)


def parse_bugs(output: str) -> list[str]:
    """Parse KASAN/BUG/Oops from output and correlate with nearest test."""
    bugs = []
    last_test = ""
    for line in output.splitlines():
        if re.match(r"^(ok|not ok)\s+\d+", line):
            last_test = line.strip()
        if re.search(r"(KASAN:|BUG:|Oops:|WARNING:|UBSAN:)", line):
            entry = line.strip()
            if last_test:
                entry = f"{entry} [near: {last_test}]"
            bugs.append(entry)
    return bugs
