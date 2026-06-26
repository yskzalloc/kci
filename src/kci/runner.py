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
    test_dirs = [d for d in install_dir.iterdir() if d.is_dir()]
    if not test_dirs:
        sys.exit(f"Error: {install_dir} has no test directories. Check build deps.")


def _parse_bugs(output: str) -> list[str]:
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


def _retry_failed_tests(runner: VMRunner, kernel: KernelSource, config: RunConfig,
                        failed_tests: list[str], retries: int) -> tuple[list[str], list[str]]:
    """Retry failed tests individually, return (still_failed, flaky)."""
    still_failed = []
    flaky = []
    for test_line in failed_tests:
        # Extract test name pattern like "net:tls" from "not ok 5 selftests: net: tls"
        m = re.search(r"selftests:\s*(\S+):\s*(\S+)", test_line)
        if not m:
            still_failed.append(test_line)
            continue
        target, name = m.group(1), m.group(2)
        filter_pat = f"{target}:{name}"
        passed_on_retry = False
        for _ in range(retries):
            run_cmd = f"./run_kselftest.sh -t {filter_pat}"
            exec_cmd = f"{KSELFTEST_SETUP}; cd kselftest_install && {run_cmd} 2>&1"
            result = runner.run(kernel, exec_cmd, config, user="root", network="user",
                                timeout=300)
            if result.stdout and "not ok" not in result.stdout:
                passed_on_retry = True
                break
        if passed_on_retry:
            flaky.append(test_line)
        else:
            still_failed.append(test_line)
    return still_failed, flaky


def run_kunit(runner: VMRunner, kernel: KernelSource, config: RunConfig) -> TestResults:
    """Run kunit tests using kunit.py via vng."""
    kernel.results_dir.mkdir(exist_ok=True)
    output = kernel.results_dir / "kunit.txt"

    print("\n--- kunit ---")
    exec_cmd = "./tools/testing/kunit/kunit.py run --raw_output"
    result = runner.run(kernel, exec_cmd, config, timeout=config.timeout_kunit)

    stdout = result.stdout or ""
    if stdout:
        output.write_text(stdout)
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

    run_cmd = "./run_kselftest.sh"
    if filter_pattern:
        run_cmd = f"./run_kselftest.sh -t {filter_pattern}"

    exec_cmd = (
        f"{KSELFTEST_SETUP}; "
        f"cd kselftest_install && {run_cmd} 2>&1 "
        "| grep -E '(^ok|^not ok|# PASS|# FAIL|# SKIP|# Totals)'; "
        "echo '=== DMESG BUGS ==='; "
        "dmesg | grep -E '(BUG:|WARNING:|UBSAN:|KASAN:|Oops:)'; "
        "echo '=== FULL DMESG ==='; "
        "dmesg"
    )
    result = runner.run(kernel, exec_cmd, config, user="root", network="user",
                        timeout=config.timeout_kselftest)

    stdout = result.stdout or ""
    if stdout:
        output.write_text(stdout)
        # Save dmesg separately
        dmesg_file = kernel.results_dir / "dmesg-kselftest.txt"
        dmesg_marker = "=== FULL DMESG ==="
        if dmesg_marker in stdout:
            dmesg_content = stdout.split(dmesg_marker, 1)[1]
            dmesg_file.write_text(dmesg_content)
        output.write_text(stdout)
    else:
        print("Warning: no kselftest output captured")

    results = _parse_kselftest_results(output)

    # KASAN/Oops correlation
    results.bugs = _parse_bugs(stdout)
    if results.bugs:
        print(f"  ⚠️  {len(results.bugs)} kernel bugs detected")

    # Per-test retry
    if config.retry > 0 and results.failed_tests:
        print(f"  Retrying {len(results.failed_tests)} failed tests (up to {config.retry}x)...")
        still_failed, flaky = _retry_failed_tests(
            runner, kernel, config, results.failed_tests, config.retry)
        results.flaky_tests = flaky
        results.failed_tests = still_failed
        results.failed = len(still_failed)
        results.passed += len(flaky)
        if flaky:
            print(f"  {len(flaky)} tests marked flaky (passed on retry)")

    return results


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
    passed = sum(1 for l in lines if re.match(r"^\s*ok\s+\d+", l))
    failed = sum(1 for l in lines if re.match(r"^\s*not ok\s+\d+", l))
    failed_names = [l.strip() for l in lines if re.match(r"^\s*not ok", l)]
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

STRESS_NG_SCRIPT_PATH = Path(__file__).parent / "scripts" / "kernel-coverage.sh"


def run_stress(runner: VMRunner, kernel: KernelSource, config: RunConfig,
               duration: int = 5) -> TestResults:
    """Run stress-ng kernel coverage test in VM."""
    kernel.results_dir.mkdir(exist_ok=True)
    output = kernel.results_dir / "stress-ng.txt"

    print(f"\n--- stress-ng (duration per stressor: {duration}s) ---")

    # Copy script and stress-ng binary into kernel tree for VM access
    dest_script = kernel.path / ".kci-stress.sh"
    script_content = STRESS_NG_SCRIPT_PATH.read_text().replace(
        'DURATION="${STRESS_DURATION:-5}"',
        f'DURATION="{duration}"'
    )
    dest_script.write_text(script_content)
    dest_script.chmod(0o755)

    # stress-ng binary path (built during init)
    stress_ng_bin = Path.home() / "stress-ng" / "stress-ng"
    exec_cmd = f"STRESS_NG={stress_ng_bin} bash .kci-stress.sh"

    result = runner.run(kernel, exec_cmd, config, user="root", network="user",
                        timeout=2400)  # 40 min max

    stdout = result.stdout or ""
    if stdout:
        output.write_text(stdout)

    # Parse: count BUG/KASAN/UBSAN in output
    bugs = [l for l in stdout.splitlines()
            if any(k in l for k in ("BUG:", "KASAN:", "UBSAN:", "Oops:"))]

    return TestResults(
        suite="stress-ng",
        passed=1 if not bugs else 0,
        failed=len(bugs),
        output_file=output,
        bugs=bugs,
    )
