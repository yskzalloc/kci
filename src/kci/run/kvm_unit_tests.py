"""Run target: kvm-unit-tests — run directly on the host."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from ..models import KernelSource, TestResults


def run(kvm_tests_dir: Path, kernel: KernelSource) -> TestResults:
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
