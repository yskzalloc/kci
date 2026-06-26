"""Test runner — orchestrates kunit, kselftest, kvm-unit-tests execution."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .models import KernelSource, RunConfig, TestResults
from .vm import VMRunner


KSELFTEST_SETUP = (
    "mount -t tmpfs tmpfs /run; "
    "mkdir -p /run/netns; "
    "ip link set lo up; "
    "ip link set eth0 up; "
    "modprobe -a veth bridge tun dummy vxlan bonding team macsec "
    "ipvlan macvlan geneve bareudp amt nf_conntrack nf_nat "
    "ip_tables ip6_tables xt_mark 2>/dev/null; "
    "sysctl -w net.ipv4.conf.all.rp_filter=0 2>/dev/null; "
    "sysctl -w net.ipv4.ping_group_range='0 2147483647' 2>/dev/null"
)


def run_kunit(runner: VMRunner, kernel: KernelSource, config: RunConfig) -> TestResults:
    """Run kunit tests via VM."""
    kernel.results_dir.mkdir(exist_ok=True)
    output = kernel.results_dir / "kunit.txt"

    print("\n--- kunit ---")
    exec_cmd = "dmesg | grep -E '(# Totals|not ok)'"
    result = runner.run(kernel, exec_cmd, config)

    # Save output
    if result.stdout:
        output.write_text(result.stdout)

    return _parse_kunit_results(output)


def run_kselftest(runner: VMRunner, kernel: KernelSource, config: RunConfig) -> TestResults:
    """Run kselftest via VM."""
    kernel.results_dir.mkdir(exist_ok=True)
    output = kernel.results_dir / "kselftest.txt"

    print(f"\n--- kselftest ({config.targets}) ---")
    exec_cmd = (
        f"{KSELFTEST_SETUP}; "
        "cd kselftest_install && ./run_kselftest.sh 2>&1 "
        "| grep -E '(^ok|^not ok|# PASS|# FAIL|# SKIP|# Totals)'"
    )
    result = runner.run(kernel, exec_cmd, config, user="root", network="bridge")

    if result.stdout:
        output.write_text(result.stdout)

    return _parse_kselftest_results(output)


def run_kvm_unit_tests(kvm_tests_dir: Path, kernel: KernelSource) -> TestResults:
    """Run kvm-unit-tests directly."""
    kernel.results_dir.mkdir(exist_ok=True)
    output = kernel.results_dir / "kvm-unit-tests.txt"

    if not (kvm_tests_dir / "x86-run").exists():
        raise FileNotFoundError("kvm-unit-tests not built. Run: kci init")

    print("\n--- kvm-unit-tests ---")
    result = subprocess.run(
        ["bash", "-c", "ACCEL=kvm ./run_tests.sh"],
        cwd=kvm_tests_dir, check=False,
        capture_output=True, text=True,
        env={**__import__("os").environ, "ACCEL": "kvm"},
    )
    # Strip ANSI color codes
    import re
    combined = re.sub(r'\x1b\[[0-9;]*m', '', (result.stdout or "") + (result.stderr or ""))
    output.write_text(combined)

    passed = sum(1 for l in combined.splitlines() if l.startswith("PASS"))
    failed = sum(1 for l in combined.splitlines() if l.startswith("FAIL"))
    skipped = sum(1 for l in combined.splitlines() if l.startswith("SKIP"))
    return TestResults(suite="kvm-unit-tests", passed=passed, failed=failed,
                       skipped=skipped, output_file=output)


def _parse_kunit_results(output: Path) -> TestResults:
    """Parse kunit output."""
    if not output.exists():
        return TestResults(suite="kunit")
    text = output.read_text()
    lines = text.splitlines()
    passed = sum(1 for l in lines if "# Totals" in l and "fail:0" in l)
    failed = sum(1 for l in lines if l.strip().startswith("not ok"))
    return TestResults(suite="kunit", passed=passed, failed=failed, output_file=output)


def _parse_kselftest_results(output: Path) -> TestResults:
    """Parse kselftest output."""
    if not output.exists():
        return TestResults(suite="kselftest")
    lines = output.read_text().splitlines()
    passed = sum(1 for l in lines if l.startswith("ok"))
    failed = sum(1 for l in lines if l.startswith("not ok"))
    skipped = sum(1 for l in lines if "# SKIP" in l)
    return TestResults(suite="kselftest", passed=passed, failed=failed,
                       skipped=skipped, output_file=output)
