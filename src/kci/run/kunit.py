"""Run target: kunit — in-kernel unit tests via kunit.py under vng."""

from __future__ import annotations

import re
from pathlib import Path

from ..models import KernelSource, RunConfig, TestResults
from ..vm import VMRunner


def run(runner: VMRunner, kernel: KernelSource, config: RunConfig) -> TestResults:
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

    return parse_results(output)


def parse_results(output: Path) -> TestResults:
    """Parse kunit output."""
    if not output.exists():
        return TestResults(suite="kunit")
    lines = output.read_text().splitlines()
    passed = sum(1 for l in lines if re.match(r"^\s*ok\s+\d+", l))
    failed = sum(1 for l in lines if re.match(r"^\s*not ok\s+\d+", l))
    failed_names = [l.strip() for l in lines if re.match(r"^\s*not ok", l)]
    return TestResults(suite="kunit", passed=passed, failed=failed,
                       output_file=output, failed_tests=failed_names)
