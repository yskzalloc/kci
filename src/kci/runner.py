"""Test runner — orchestrates kunit, kselftest, kvm-unit-tests execution."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from .models import KernelSource, RunConfig, TestResults
from .vm import VMRunner


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


def validate_kselftest_install(kernel: KernelSource) -> None:
    """Check kselftest_install/ exists and has test files."""
    install_dir = kernel.path / "kselftest_install"
    if not install_dir.exists():
        sys.exit(f"Error: {install_dir} not found. Run: kci build")
    run_script = install_dir / "run_kselftest.sh"
    if not run_script.exists():
        sys.exit(f"Error: {run_script} missing. Rebuild with: kci build")
    # Check at least one target dir has files
    test_dirs = [d for d in install_dir.iterdir() if d.is_dir()]
    if not test_dirs:
        sys.exit(f"Error: {install_dir} has no test directories. Check build deps.")


def run_kunit(runner: VMRunner, kernel: KernelSource, config: RunConfig) -> TestResults:
    """Run kunit tests via vng boot (kunit runs at boot via CONFIG_KUNIT)."""
    kernel.results_dir.mkdir(exist_ok=True)
    output = kernel.results_dir / "kunit.txt"

    print("\n--- kunit ---")
    exec_cmd = "dmesg | grep -E '(# Totals|not ok)'"
    result = runner.run(kernel, exec_cmd, config, timeout=config.timeout_kunit)

    stdout = result.stdout or ""
    if stdout:
        output.write_text(stdout)
        print(stdout)
    else:
        print("Warning: no kunit output captured")

    return _parse_kunit_results(output)


def run_kselftest(runner: VMRunner, kernel: KernelSource, config: RunConfig,
                  filter_pattern: str | None = None) -> TestResults:
    """Run kselftest via VM."""
    kernel.results_dir.mkdir(exist_ok=True)
    output = kernel.results_dir / "kselftest.txt"

    validate_kselftest_install(kernel)

    print(f"\n--- kselftest ({config.targets}) ---")
    if filter_pattern:
        print(f"    filter: {filter_pattern}")

    # Build the run command with optional filter
    run_cmd = "./run_kselftest.sh"
    if filter_pattern:
        run_cmd = f"./run_kselftest.sh -t {filter_pattern}"

    exec_cmd = (
        f"{KSELFTEST_SETUP}; "
        f"cd kselftest_install && {run_cmd} 2>&1 "
        "| grep -E '(^ok|^not ok|# PASS|# FAIL|# SKIP|# Totals)'; "
        "echo '=== DMESG BUGS ==='; "
        "dmesg | grep -E '(BUG:|WARNING:|UBSAN:|KASAN:)'"
    )
    result = runner.run(kernel, exec_cmd, config, user="root", network="user",
                        timeout=config.timeout_kselftest)

    stdout = result.stdout or ""
    if stdout:
        output.write_text(stdout)
        print(stdout)
    else:
        print("Warning: no kselftest output captured")

    return _parse_kselftest_results(output)


def run_kvm_unit_tests(kvm_tests_dir: Path, kernel: KernelSource) -> TestResults:
    """Run kvm-unit-tests directly."""
    kernel.results_dir.mkdir(exist_ok=True)
    output = kernel.results_dir / "kvm-unit-tests.txt"

    if not (kvm_tests_dir / "x86-run").exists():
        sys.exit("Error: kvm-unit-tests not built. Run: kci init")

    print("\n--- kvm-unit-tests ---")
    result = subprocess.run(
        ["bash", "-c", "ACCEL=kvm ./run_tests.sh"],
        cwd=kvm_tests_dir, check=False,
        capture_output=True, text=True,
        env={**os.environ, "ACCEL": "kvm"},
    )
    combined = re.sub(r'\x1b\[[0-9;]*m', '', (result.stdout or "") + (result.stderr or ""))
    output.write_text(combined)
    print(combined)

    passed = sum(1 for l in combined.splitlines() if l.startswith("PASS"))
    failed = sum(1 for l in combined.splitlines() if l.startswith("FAIL"))
    skipped = sum(1 for l in combined.splitlines() if l.startswith("SKIP"))
    return TestResults(suite="kvm-unit-tests", passed=passed, failed=failed,
                       skipped=skipped, output_file=output)


def _parse_kunit_results(output: Path) -> TestResults:
    """Parse kunit output."""
    if not output.exists():
        return TestResults(suite="kunit")
    lines = output.read_text().splitlines()
    passed = sum(1 for l in lines if "# Totals" in l and "fail:0" in l)
    failed = sum(1 for l in lines if l.strip().startswith("not ok"))
    failed_names = [l.strip() for l in lines if l.strip().startswith("not ok")]
    return TestResults(suite="kunit", passed=passed, failed=failed,
                       output_file=output, failed_tests=failed_names)


def _parse_kselftest_results(output: Path) -> TestResults:
    """Parse kselftest output."""
    if not output.exists():
        return TestResults(suite="kselftest")
    lines = output.read_text().splitlines()
    passed = sum(1 for l in lines if l.startswith("ok"))
    failed = sum(1 for l in lines if l.startswith("not ok"))
    skipped = sum(1 for l in lines if "# SKIP" in l)
    failed_names = [l for l in lines if l.startswith("not ok")]
    return TestResults(suite="kselftest", passed=passed, failed=failed,
                       skipped=skipped, output_file=output, failed_tests=failed_names)
