"""Run target: kselftest — optionally piggybacks kunit, stress-ng and
kvm-unit-tests in the same VM boot to save boot time on CI."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from ..models import KernelSource, RunConfig, TestResults
from ..vm import VMRunner
from . import kunit, stress_ng
from .common import KSELFTEST_SETUP, parse_bugs

# kselftests that take down the VM itself rather than fail: the suspend
# test puts the QEMU guest into deep suspend and nothing ever wakes it,
# so the boot dies mid-run and every later suite's output is lost.
SKIP_TESTS = ("breakpoints:step_after_suspend_test",)

# In-guest wall-clock cap for the stress-ng coverage script; its phase
# durations are scaled to ~2h total (see scripts/kernel-coverage.sh).
STRESS_NG_TIMEOUT = 7200


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
        suites: tuple[str, ...] | list[str] = ("kselftest", "kvm-unit-tests", "stress-ng"),
        ) -> tuple[TestResults | None, TestResults | None, TestResults | None, TestResults | None]:
    """Run the given suites in a single VM boot, in the given order.

    "kunit" is the exception: it executes during kernel boot itself, so its
    part (which only captures the TAP output from dmesg) always goes first."""
    kernel.results_dir.mkdir(exist_ok=True)
    output = kernel.results_dir / "kselftest.txt"
    kunit_output = kernel.results_dir / "kunit.txt"

    include_kunit = "kunit" in suites
    include_kselftest = "kselftest" in suites
    include_kvm = "kvm-unit-tests" in suites
    include_stress_ng = "stress-ng" in suites

    if include_kselftest:
        validate_install(kernel)

    print(f"\n--- single boot: {' + '.join(suites)} ---")
    if filter_pattern:
        print(f"    filter: {filter_pattern}")

    # Each part is self-contained (sets its own cwd) so they can be
    # concatenated in any order.
    parts: dict[str, str] = {}

    if include_kunit:
        parts["kunit"] = (
            "echo '=== KUNIT START ==='; "
            "dmesg | grep -E '(# Totals|not ok|ok [0-9])'; "
            "echo '=== KUNIT END ==='; "
        )

    if include_kselftest:
        skip_args = "".join(f" -S {t}" for t in SKIP_TESTS)
        run_cmd = f"./run_kselftest.sh{skip_args}"
        if filter_pattern:
            run_cmd = f"./run_kselftest.sh -t {filter_pattern}"
        parts["kselftest"] = (
            f"cd {kernel.path}; "
            "echo '=== KSELFTEST START ==='; "
            f"cd kselftest_install && {run_cmd} 2>&1 || true; "
            "echo '=== KSELFTEST END ==='; "
        )

    # kvm-unit-tests part (nested KVM inside vng)
    kvm_tests_dir = Path.home() / "kvm-unit-tests"
    if include_kvm and kvm_tests_dir.exists() and (kvm_tests_dir / "x86-run").exists():
        parts["kvm-unit-tests"] = (
            "echo '=== KVM-UNIT-TESTS START ==='; "
            f"cd {kvm_tests_dir} && ACCEL=kvm ./run_tests.sh 2>&1 || true; "
            "echo '=== KVM-UNIT-TESTS END ==='; "
        )

    if include_stress_ng:
        stress_ng_bin = stress_ng.prepare_script(kernel)
        # run from /root so stressor temp files stay off the kernel tree;
        # the script path must be absolute for that to work.  The in-guest
        # timeout confines an overrun to the stress section so the later
        # suites and the END markers still execute.
        parts["stress-ng"] = (
            "cd /root; "
            "echo '=== STRESS START ==='; "
            f"STRESS_NG={stress_ng_bin} timeout -k 60 {STRESS_NG_TIMEOUT} "
            f"bash {kernel.path}/.kci-stress.sh || true; "
            "echo '=== STRESS END ==='; "
        )

    # kunit (boot-time) first, then the requested order
    ordered = [s for s in suites if s == "kunit" and s in parts]
    ordered += [s for s in suites if s != "kunit" and s in parts]

    exec_cmd = (
        f"{KSELFTEST_SETUP}; "
        + "".join(parts[s] for s in ordered)
        + "echo '=== DMESG BUGS ==='; "
        "dmesg | grep -E '(BUG:|WARNING:|UBSAN:|KASAN:|Oops:)'; "
        "echo '=== FULL DMESG ==='; "
        "dmesg"
    )

    total_timeout = config.timeout_kselftest + (
        STRESS_NG_TIMEOUT + 300 if include_stress_ng else 0)
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

    results = None
    if include_kselftest:
        if kselftest_text:
            output.write_text(kselftest_text)
            print(kselftest_text[-2000:] if len(kselftest_text) > 2000 else kselftest_text)
        else:
            print("Warning: no kselftest output captured")

        results = parse_results(output)
        results.bugs = parse_bugs(stdout)
        if results.bugs:
            print(f"  ⚠️  {len(results.bugs)} kernel bugs detected")

    # Parse stress-ng results
    stress_ng_results = None
    if include_stress_ng and "=== STRESS START ===" in stdout and "=== STRESS END ===" in stdout:
        stress_ng_text = stdout.split("=== STRESS START ===")[1].split("=== STRESS END ===")[0]
        stress_ng_results = stress_ng.parse_results(kernel, stress_ng_text)
        print(f"\n--- stress-ng: {stress_ng_results.failed} bugs detected ---")

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
    if config.retry > 0 and results and results.failed_tests:
        print(f"  Retrying {len(results.failed_tests)} failed tests (up to {config.retry}x)...")
        still_failed, flaky = _retry_failed_tests(
            runner, kernel, config, results.failed_tests, config.retry)
        results.flaky_tests = flaky
        results.failed_tests = still_failed
        results.failed = len(still_failed)
        results.passed += len(flaky)
        if flaky:
            print(f"  {len(flaky)} tests marked flaky (passed on retry)")

    return results, stress_ng_results, (kunit.parse_results(kunit_output) if include_kunit else None), kvm_results


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
