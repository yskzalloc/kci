"""Run target: kselftest — optionally piggybacks kunit, stress-ng and
kvm-unit-tests in the same VM boot to save boot time on CI."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from ..models import KernelSource, RunConfig, TestResults
from ..vm import VMRunner
from . import kunit, stress
from .common import KSELFTEST_SETUP, parse_bugs


def validate_install(kernel: KernelSource) -> None:
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


def run(runner: VMRunner, kernel: KernelSource, config: RunConfig,
        filter_pattern: str | None = None,
        include_stress: bool = False,
        include_kunit: bool = False,
        ) -> tuple[TestResults, TestResults | None, TestResults | None, TestResults | None]:
    """Run kselftest (and optionally kunit + stress-ng) in a single VM boot."""
    kernel.results_dir.mkdir(exist_ok=True)
    output = kernel.results_dir / "kselftest.txt"
    kunit_output = kernel.results_dir / "kunit.txt"

    validate_install(kernel)

    print(f"\n--- single boot: {'kunit + ' if include_kunit else ''}kselftest{' + stress' if include_stress else ''} ---")
    if filter_pattern:
        print(f"    filter: {filter_pattern}")

    run_cmd = "./run_kselftest.sh"
    if filter_pattern:
        run_cmd = f"./run_kselftest.sh -t {filter_pattern}"

    # kunit part (kunit runs at boot via CONFIG_KUNIT, extract from dmesg)
    kunit_part = ""
    if include_kunit:
        kunit_part = (
            "echo '=== KUNIT START ==='; "
            "dmesg | grep -E '(# Totals|not ok|ok [0-9])'; "
            "echo '=== KUNIT END ==='; "
        )

    # Stress part
    stress_part = ""
    if include_stress:
        stress_ng_bin = stress.prepare_script(kernel)
        stress_part = (
            "echo '=== STRESS START ==='; "
            f"STRESS_NG={stress_ng_bin} bash .kci-stress.sh || true; "
            "echo '=== STRESS END ==='; "
        )

    # kvm-unit-tests part (nested KVM inside vng)
    kvm_tests_dir = Path.home() / "kvm-unit-tests"
    kvm_part = ""
    if kvm_tests_dir.exists() and (kvm_tests_dir / "x86-run").exists():
        kvm_part = (
            "echo '=== KVM-UNIT-TESTS START ==='; "
            f"cd {kvm_tests_dir} && ACCEL=kvm ./run_tests.sh 2>&1 || true; "
            "echo '=== KVM-UNIT-TESTS END ==='; "
        )

    exec_cmd = (
        f"{KSELFTEST_SETUP}; "
        f"{kunit_part}"
        "echo '=== KSELFTEST START ==='; "
        f"cd kselftest_install && {run_cmd} 2>&1 || true; "
        "echo '=== KSELFTEST END ==='; "
        f"cd / && {kvm_part}"
        f"cd /root && {stress_part}"
        "echo '=== DMESG BUGS ==='; "
        "dmesg | grep -E '(BUG:|WARNING:|UBSAN:|KASAN:|Oops:)'; "
        "echo '=== FULL DMESG ==='; "
        "dmesg"
    )

    total_timeout = config.timeout_kselftest + (3900 if include_stress else 0)
    result = runner.run(kernel, exec_cmd, config, user="root", network="user",
                        timeout=total_timeout)

    stdout = result.stdout or ""

    # Always save the full raw vng output
    raw_output = kernel.results_dir / "vng-output.txt"
    if stdout:
        raw_output.write_text(stdout)

    # Split kselftest output (between markers)
    kselftest_text = ""
    if "=== KSELFTEST START ===" in stdout and "=== KSELFTEST END ===" in stdout:
        kselftest_text = stdout.split("=== KSELFTEST START ===")[1].split("=== KSELFTEST END ===")[0]

    # Parse kunit output if included
    if include_kunit and "=== KUNIT START ===" in stdout and "=== KUNIT END ===" in stdout:
        kunit_text = stdout.split("=== KUNIT START ===")[1].split("=== KUNIT END ===")[0]
        kunit_output.write_text(kunit_text)

    # Save dmesg
    if "=== FULL DMESG ===" in stdout:
        dmesg_content = stdout.split("=== FULL DMESG ===")[1]
        (kernel.results_dir / "dmesg.txt").write_text(dmesg_content)

    if kselftest_text:
        output.write_text(kselftest_text)
        print(kselftest_text[-2000:] if len(kselftest_text) > 2000 else kselftest_text)
    else:
        print("Warning: no kselftest output captured")

    results = parse_results(output)
    results.bugs = parse_bugs(stdout)
    if results.bugs:
        print(f"  ⚠️  {len(results.bugs)} kernel bugs detected")

    # Parse stress results
    stress_results = None
    if include_stress and "=== STRESS START ===" in stdout and "=== STRESS END ===" in stdout:
        stress_text = stdout.split("=== STRESS START ===")[1].split("=== STRESS END ===")[0]
        stress_results = stress.parse_results(kernel, stress_text)
        print(f"\n--- stress-ng: {stress_results.failed} bugs detected ---")

    # Parse kvm-unit-tests results
    kvm_results = None
    if "=== KVM-UNIT-TESTS START ===" in stdout and "=== KVM-UNIT-TESTS END ===" in stdout:
        kvm_text = stdout.split("=== KVM-UNIT-TESTS START ===")[1].split("=== KVM-UNIT-TESTS END ===")[0]
        # Strip ANSI codes
        kvm_text = re.sub(r'\x1b\[[0-9;]*m', '', kvm_text)
        kvm_output = kernel.results_dir / "kvm-unit-tests.txt"
        kvm_output.write_text(kvm_text)
        kvm_lines = kvm_text.splitlines()
        kvm_results = TestResults(
            suite="kvm-unit-tests",
            passed=sum(1 for l in kvm_lines if l.startswith("PASS")),
            failed=sum(1 for l in kvm_lines if l.startswith("FAIL")),
            skipped=sum(1 for l in kvm_lines if l.startswith("SKIP")),
            output_file=kvm_output,
            failed_tests=[l for l in kvm_lines if l.startswith("FAIL")],
        )

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

    return results, stress_results, (kunit.parse_results(kunit_output) if include_kunit else None), kvm_results


def parse_results(output: Path) -> TestResults:
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
