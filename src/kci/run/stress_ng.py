"""Run target: stress-ng kernel coverage."""

from __future__ import annotations

from pathlib import Path

from ..models import KernelSource, RunConfig, TestResults
from ..vm import VMRunner
from .common import SCRIPTS_DIR

SCRIPT_PATH = SCRIPTS_DIR / "kernel-coverage.sh"

BUG_KEYS = ("BUG:", "KASAN:", "UBSAN:", "Oops:")


def prepare_script(kernel: KernelSource, duration: int | None = None) -> Path:
    """Copy the stress script into the kernel tree for VM access.

    Returns the stress-ng binary path (built during kci init)."""
    dest_script = kernel.path / ".kci-stress.sh"
    script_content = SCRIPT_PATH.read_text()
    if duration is not None:
        script_content = script_content.replace(
            'DURATION="${STRESS_DURATION:-5}"',
            f'DURATION="{duration}"'
        )
    dest_script.write_text(script_content)
    dest_script.chmod(0o755)
    return Path.home() / "stress-ng" / "stress-ng"


def parse_results(kernel: KernelSource, stress_text: str) -> TestResults:
    """Parse stress-ng output text, save it, and count kernel bugs."""
    output = kernel.results_dir / "stress-ng.txt"
    output.write_text(stress_text)
    bugs = [l for l in stress_text.splitlines()
            if any(k in l for k in BUG_KEYS)]
    return TestResults(
        suite="stress-ng",
        passed=1 if not bugs else 0,
        failed=len(bugs),
        output_file=output,
        bugs=bugs,
    )


def run(runner: VMRunner, kernel: KernelSource, config: RunConfig,
        duration: int = 5) -> TestResults:
    """Run stress-ng kernel coverage test in VM."""
    kernel.results_dir.mkdir(exist_ok=True)

    print(f"\n--- stress-ng (duration per stressor: {duration}s) ---")

    stress_ng_bin = prepare_script(kernel, duration)
    exec_cmd = f"STRESS_NG={stress_ng_bin} bash .kci-stress.sh"

    result = runner.run(kernel, exec_cmd, config, user="root", network="user",
                        timeout=3900)  # 65 min max

    return parse_results(kernel, result.stdout or "")
