"""KCIDB submission generator.

Exports test-results/ as a KCIDB I/O schema v5.3 report with the full
checkout -> build -> tests object tree, suitable for `kcidb-submit` /
kci-dev or for sharing as a sample payload when requesting a token
(https://docs.kernelci.org/components/kcidb/submitter_guide/).
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .models import KernelSource

SCHEMA_VERSION = {"major": 5, "minor": 3}

# dmesg decorations on TAP lines captured from the guest console:
# "[  104.285994] [    T1] not ok 5 foo" -> "not ok 5 foo"
_DMESG_PREFIX_RE = re.compile(r"^(\[[^\]]*\]\s*)+")
# test.path components must match [a-zA-Z0-9_-]+ joined by dots
_PATH_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9_-]+")

_KSELFTEST_RE = re.compile(r"^(not )?ok\s+\d+\s+selftests:\s*(\S+?):?\s+(\S+)")
_TAP_RE = re.compile(r"^(not )?ok\s+(\d+)\s*-?\s*(.*)$")
_KVM_RE = re.compile(r"^(PASS|FAIL|SKIP)\s+(\S+)\s*(.*)$")
_KUNIT_TOTALS_RE = re.compile(
    r"# Totals: pass:(\d+) fail:(\d+) skip:(\d+) total:(\d+)")


def _path(*components: str) -> str:
    return ".".join(
        _PATH_SANITIZE_RE.sub("_", c).strip("_") or "_" for c in components)


def _git(kernel: KernelSource, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(kernel.path), *args],
                             capture_output=True, text=True, check=True)
        return out.stdout.strip() or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _file_time(path: Path) -> str | None:
    if not path.exists():
        return None
    return datetime.fromtimestamp(
        path.stat().st_mtime, tz=timezone.utc).isoformat()


def _log_url() -> str | None:
    """GitHub Actions run URL, when running in CI."""
    server = os.environ.get("GITHUB_SERVER_URL")
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if server and repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return None


def _run_id() -> str:
    """Unique-enough run discriminator for object ids."""
    gh = os.environ.get("GITHUB_RUN_ID")
    if gh:
        attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
        return f"{gh}_{attempt}"
    return datetime.now(tz=timezone.utc).strftime("%Y%m%d%H%M%S")


def _compiler(kernel: KernelSource) -> str | None:
    config = kernel.path / ".config"
    if not config.exists():
        return None
    for line in config.read_text().splitlines():
        if line.startswith("CONFIG_CC_VERSION_TEXT="):
            # CONFIG_CC_VERSION_TEXT="Debian clang version 19.1.7 (3)"
            text = line.split("=", 1)[1].strip().strip('"')
            m = re.search(r"(clang|gcc)[^,(]*?([\d.]+)", text)
            return f"{m.group(1)} {m.group(2)}" if m else text
    return None


def _strip_dmesg(line: str) -> str:
    return _DMESG_PREFIX_RE.sub("", line).strip()


def _checkout(kernel: KernelSource, origin: str,
              tree_name: str | None) -> dict:
    commit = _git(kernel, "rev-parse", "HEAD")
    checkout = {
        "id": f"{origin}:{commit or _run_id()}",
        "origin": origin,
        "valid": True,
        "patchset_hash": "",
    }
    if commit:
        checkout["git_commit_hash"] = commit
    name = _git(kernel, "describe", "--always", "--tags")
    if name:
        checkout["git_commit_name"] = name
    branch = _git(kernel, "rev-parse", "--abbrev-ref", "HEAD")
    if branch and branch != "HEAD":
        checkout["git_repository_branch"] = branch
    url = _git(kernel, "remote", "get-url", "origin")
    if url and url.startswith(("http://", "https://")):
        checkout["git_repository_url"] = url
        if not tree_name:
            tree_name = Path(url).stem  # ".../linux-dept.git" -> "linux-dept"
    if tree_name:
        checkout["tree_name"] = tree_name
    commit_time = _git(kernel, "log", "-1", "--format=%cI")
    if commit_time:
        checkout["start_time"] = commit_time
    return checkout


def _build(kernel: KernelSource, origin: str, checkout_id: str,
           config_name: str | None, arch: str) -> dict:
    build = {
        "id": f"{origin}:build:{_run_id()}:{config_name or 'default'}",
        "origin": origin,
        "checkout_id": checkout_id,
        "architecture": arch,
        # results exist, so the build completed
        "status": "PASS",
    }
    if config_name:
        build["config_name"] = config_name
    compiler = _compiler(kernel)
    if compiler:
        build["compiler"] = compiler
    start = _file_time(kernel.path / ".config")
    if start:
        build["start_time"] = start
    log_url = _log_url()
    if log_url:
        build["log_url"] = log_url
    return build


def _test(origin: str, build_id: str, seq: int, path: str, status: str,
          start_time: str | None = None, comment: str | None = None,
          misc: dict | None = None) -> dict:
    test = {
        "id": f"{build_id}:{seq}",
        "origin": origin,
        "build_id": build_id,
        "path": path,
        "status": status,
        "environment": {"comment": "virtme-ng / QEMU x86_64 guest"},
    }
    if start_time:
        test["start_time"] = start_time
    if comment:
        test["comment"] = comment[:256]
    if misc:
        test["misc"] = misc
    return test


def _kunit_tests(results_dir: Path, add) -> None:
    f = results_dir / "kunit.txt"
    if not f.exists():
        return
    start = _file_time(f)
    totals = [0, 0, 0, 0]
    any_line = False
    for raw in f.read_text().splitlines():
        line = _strip_dmesg(raw)
        m = _KUNIT_TOTALS_RE.search(line)
        if m:
            any_line = True
            for i in range(4):
                totals[i] += int(m.group(i + 1))
            continue
        m = _TAP_RE.match(line)
        if not m:
            continue
        any_line = True
        name = m.group(3).split("# SKIP")[0].strip()
        if "# SKIP" in line:
            status = "SKIP"
        elif m.group(1):
            status = "FAIL"
        else:
            # the boot-time capture only greps failures + totals; individual
            # "ok" lines are suite-level results
            status = "PASS"
        add(_path("kunit", name), status, start, line)
    if any_line:
        add("kunit", "FAIL" if totals[1] else "PASS", start,
            f"kunit totals: pass:{totals[0]} fail:{totals[1]} "
            f"skip:{totals[2]} total:{totals[3]}",
            {"pass": totals[0], "fail": totals[1],
             "skip": totals[2], "total": totals[3]})


def _kselftest_tests(results_dir: Path, add) -> None:
    f = results_dir / "kselftest.txt"
    if not f.exists():
        return
    start = _file_time(f)
    for raw in f.read_text().splitlines():
        line = _strip_dmesg(raw)
        m = _KSELFTEST_RE.match(line)
        if not m:
            continue
        if "# SKIP" in line:
            status = "SKIP"
        elif m.group(1):
            status = "FAIL"
        else:
            status = "PASS"
        add(_path("kselftest", m.group(2), m.group(3)), status, start, line)


def _kvm_tests(results_dir: Path, add) -> None:
    f = results_dir / "kvm-unit-tests.txt"
    if not f.exists():
        return
    start = _file_time(f)
    for raw in f.read_text().splitlines():
        m = _KVM_RE.match(raw.strip())
        if not m:
            continue
        add(_path("kvm-unit-tests", m.group(2)), m.group(1), start,
            m.group(3).strip() or None)


def _stress_test(results_dir: Path, add) -> None:
    f = results_dir / "stress-ng.txt"
    if not f.exists():
        return
    bug_keys = ("BUG:", "KASAN:", "KCSAN:", "KMSAN:", "UBSAN:", "Oops:")
    bugs = [l for l in f.read_text().splitlines()
            if any(k in l for k in bug_keys)]
    add("stress-ng", "FAIL" if bugs else "PASS", _file_time(f),
        f"{len(bugs)} kernel bug reports during stress-ng" if bugs else None,
        {"bugs": bugs[:20]} if bugs else None)


def _boot_test(results_dir: Path, add) -> None:
    f = results_dir / "dmesg.txt"
    if not f.exists():
        return
    # dmesg was collected from inside the guest, so it booted to userspace
    add("boot", "PASS", _file_time(f))


def generate_submission(kernel: KernelSource, origin: str = "kci",
                        config_name: str | None = None,
                        tree_name: str | None = None,
                        arch: str = "x86_64") -> dict:
    """Build a KCIDB v5.3 report dict from kernel.results_dir."""
    checkout = _checkout(kernel, origin, tree_name)
    build = _build(kernel, origin, checkout["id"], config_name, arch)

    tests: list[dict] = []

    def add(path, status, start_time=None, comment=None, misc=None):
        tests.append(_test(origin, build["id"], len(tests), path, status,
                           start_time, comment, misc))

    results_dir = kernel.results_dir
    _boot_test(results_dir, add)
    _kunit_tests(results_dir, add)
    _kselftest_tests(results_dir, add)
    _kvm_tests(results_dir, add)
    _stress_test(results_dir, add)

    return {
        "version": SCHEMA_VERSION,
        "checkouts": [checkout],
        "builds": [build],
        "tests": tests,
    }
