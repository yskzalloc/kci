"""Run target: ksmbd — xfstests through cifs.ko against the in-kernel SMB server.

Boots the built kernel (CONFIG_SMB_SERVER=y, CONFIG_CIFS=y) in vng, starts
ksmbd.mountd inside the guest, then mounts //127.0.0.1 shares with cifs.ko
and runs xfstests over the loopback.

References:
  - https://wiki.samba.org/index.php/Xfstesting-cifs
  - ksmbd out-of-tree CI (.github/workflows/c-cpp.yml)

Requires on the host (shared into the guest by vng):
  - xfstests-dev built at ~/xfstests-dev (override: KCI_XFSTESTS_DIR)
  - ksmbd-tools installed (ksmbd.mountd, ksmbd.adduser)
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from ..models import KernelSource, RunConfig, TestResults
from ..vm import VMRunner
from .common import SCRIPTS_DIR, parse_bugs

SCRIPT_PATH = SCRIPTS_DIR / "xfstests-ksmbd.sh"

# Storage-light quick set known to work against ksmbd, taken from the ksmbd
# out-of-tree CI and the samba wiki regression list. GitHub-hosted runners
# only have ~14GB free, so large-file and ENOSPC tests are left out
# (generic/011 also excluded: it needs patching to shrink its dataset).
DEFAULT_TESTS = (
    "cifs/001 "
    "generic/001 generic/002 generic/005 generic/006 generic/007 generic/008 "
    "generic/010 generic/013 generic/014 generic/023 generic/024 generic/028 "
    "generic/029 generic/030 generic/032 generic/033 generic/036 generic/037 "
    "generic/043 generic/044 generic/045 generic/046 generic/051 generic/069 "
    "generic/070 generic/071 generic/072 generic/074 generic/080 generic/084 "
    "generic/086 generic/091 generic/095 generic/098 generic/100 generic/103 "
    "generic/109 generic/113 generic/117 generic/124 generic/125 generic/129 "
    "generic/130 generic/132 generic/133 generic/135 generic/141 generic/169 "
    "generic/198 generic/207 generic/208 generic/210 generic/211 generic/212 "
    "generic/214 generic/215 generic/221 generic/225 generic/228 generic/236 "
    "generic/239 generic/241 generic/245 generic/246 generic/247 generic/248 "
    "generic/249 generic/257 generic/258 generic/263 generic/308 generic/309 "
    "generic/310 generic/313 generic/315 generic/316 generic/323 generic/337 "
    "generic/339 generic/340 generic/344 generic/345 generic/346 generic/349 "
    "generic/350 generic/354 generic/360 generic/377 generic/391 generic/393 "
    "generic/394 generic/406 generic/412 generic/420 generic/428 generic/430 "
    "generic/431 generic/432 generic/433 generic/436 generic/437 generic/438 "
    "generic/439 generic/443 generic/445 generic/446 generic/448 generic/451 "
    "generic/452 generic/454 generic/460 generic/461 generic/464 generic/465 "
    "generic/469 generic/504 generic/523 generic/524 generic/528 generic/532 "
    "generic/533 generic/539 generic/565 generic/567 generic/568 generic/599"
)


def _xfstests_dir() -> Path:
    return Path(os.environ.get("KCI_XFSTESTS_DIR", str(Path.home() / "xfstests-dev")))


def run(runner: VMRunner, kernel: KernelSource, config: RunConfig,
        tests: str | None = None) -> TestResults:
    """Run xfstests over cifs.ko against ksmbd inside the VM."""
    kernel.results_dir.mkdir(exist_ok=True)
    output = kernel.results_dir / "xfstests-ksmbd.txt"

    xfstests = _xfstests_dir()
    if not (xfstests / "check").exists():
        sys.exit(f"Error: xfstests not found at {xfstests} "
                 "(set KCI_XFSTESTS_DIR or clone/build xfstests-dev)")

    dest_script = kernel.path / ".kci-ksmbd.sh"
    dest_script.write_text(SCRIPT_PATH.read_text())
    dest_script.chmod(0o755)

    test_list = tests or DEFAULT_TESTS
    print("\n--- ksmbd: xfstests over cifs.ko ---")
    print(f"    xfstests: {xfstests}")
    print(f"    tests: {len(test_list.split())}")

    exec_cmd = (
        f"XFSTESTS_DIR={xfstests} TESTS='{test_list}' bash .kci-ksmbd.sh; "
        "echo '=== FULL DMESG ==='; dmesg"
    )
    result = runner.run(kernel, exec_cmd, config, user="root", network="user",
                        timeout=config.timeout_xfstests)

    stdout = result.stdout or ""
    if stdout:
        output.write_text(stdout)
        (kernel.results_dir / "dmesg.txt").write_text(
            stdout.split("=== FULL DMESG ===")[1] if "=== FULL DMESG ===" in stdout else "")
    else:
        print("Warning: no xfstests output captured")

    results = parse_results(output)
    results.bugs = parse_bugs(stdout)
    if results.bugs:
        print(f"  ⚠️  {len(results.bugs)} kernel bugs detected")
    return results


def parse_results(output: Path) -> TestResults:
    """Parse xfstests ./check summary.

    check prints e.g.:
        Failures: generic/030 generic/095
        Failed 2 of 84 tests
    or:
        Passed all 84 tests
    """
    if not output.exists():
        return TestResults(suite="xfstests-ksmbd")
    text = output.read_text()

    failed_tests: list[str] = []
    passed = failed = skipped = 0

    m = re.search(r"^Failures:\s+(.+)$", text, re.MULTILINE)
    if m:
        failed_tests = m.group(1).split()
    m = re.search(r"^Not run:\s+(.+)$", text, re.MULTILINE)
    if m:
        skipped = len(m.group(1).split())
    m = re.search(r"^Failed (\d+) of (\d+) tests", text, re.MULTILINE)
    if m:
        failed = int(m.group(1))
        passed = int(m.group(2)) - failed
    else:
        m = re.search(r"^Passed all (\d+) tests", text, re.MULTILINE)
        if m:
            passed = int(m.group(1))

    return TestResults(suite="xfstests-ksmbd", passed=passed, failed=failed,
                       skipped=skipped, output_file=output,
                       failed_tests=failed_tests)
