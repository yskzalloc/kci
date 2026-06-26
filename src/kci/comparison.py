"""KernelCI upstream comparison and regression detection."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .models import KernelSource, TestResults

UPSTREAM_TREES = {
    "mainline/master": ("https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git", "master"),
    "next/master": ("https://git.kernel.org/pub/scm/linux/kernel/git/next/linux-next.git", "master"),
    "stable-rc/linux-5.10.y": ("https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux-stable-rc.git", "linux-5.10.y"),
    "stable-rc/linux-5.15.y": ("https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux-stable-rc.git", "linux-5.15.y"),
    "stable-rc/linux-6.1.y": ("https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux-stable-rc.git", "linux-6.1.y"),
    "stable-rc/linux-6.6.y": ("https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux-stable-rc.git", "linux-6.6.y"),
    "stable-rc/linux-6.12.y": ("https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux-stable-rc.git", "linux-6.12.y"),
    "stable-rc/linux-6.18.y": ("https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux-stable-rc.git", "linux-6.18.y"),
    "net-next/main": ("https://git.kernel.org/pub/scm/linux/kernel/git/netdev/net-next.git", "main"),
    "tip/master": ("https://git.kernel.org/pub/scm/linux/kernel/git/tip/tip.git", "master"),
}


def fetch_upstream_failures(kcidev: Path, kernel: KernelSource, arch: str = "x86_64") -> set[str]:
    """Fetch known failures from KernelCI dashboard."""
    kernel.results_dir.mkdir(exist_ok=True)
    output = kernel.results_dir / "upstream-failures.txt"
    failures: set[str] = set()

    print("\n--- upstream comparison (kci-dev) ---")
    for name, (url, branch) in UPSTREAM_TREES.items():
        print(f"[{name}]")
        result = subprocess.run(
            f"{kcidev} results tests --giturl {url} --branch {branch} "
            f"--latest --arch {arch} --status fail",
            shell=True, capture_output=True, text=True, check=False,
        )
        for line in (result.stdout or "").splitlines():
            if "test path:" in line:
                failures.add(line.split("test path:")[-1].strip())

    output.write_text("\n".join(sorted(failures)) + "\n" if failures else "")
    return failures


def detect_regressions(kernel: KernelSource, upstream_failures: set[str]) -> list[str]:
    """Compare local failures against upstream to find regressions."""
    kselftest_file = kernel.results_dir / "kselftest.txt"
    local_failures: set[str] = set()

    if kselftest_file.exists():
        for line in kselftest_file.read_text().splitlines():
            if line.startswith("not ok"):
                local_failures.add(line)

    # Save
    local_file = kernel.results_dir / "local-failures.txt"
    local_file.write_text("\n".join(sorted(local_failures)) + "\n")

    regressions = sorted(local_failures - upstream_failures)
    return regressions


def print_summary(results: list[TestResults], regressions: list[str], upstream_count: int):
    """Print test summary."""
    print("\n--- regression analysis ---")
    for r in results:
        print(f"{r.suite}: {r.passed} pass, {r.failed} fail, {r.skipped} skip")
    print(f"upstream known failures: {upstream_count}")
    print()

    if regressions:
        print("⚠️  Potential regressions (NOT seen upstream):")
        for f in regressions[:30]:
            print(f"  {f}")
    else:
        print("✅ No new regressions — all local failures match upstream")
