"""CLI interface — git-like subcommands for kci."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .config import resolve_config, validate_config
from .comparison import detect_regressions, fetch_upstream_failures, print_summary
from .models import KernelConfig, KernelSource, RunConfig, TestResults
from .runner import run_kunit, run_kselftest, run_kvm_unit_tests
from .vm import VirtmeRunner

HOME = Path.home()
VNG_PATH = HOME / "venv-virtme" / "bin" / "vng"
KCIDEV_PATH = HOME / "venv-virtme" / "bin" / "kci-dev"
KVM_UNIT_TESTS_DIR = HOME / "kvm-unit-tests"


def _check_binaries() -> None:
    """Check required binaries exist."""
    if not VNG_PATH.exists():
        sys.exit(f"Error: vng not found at {VNG_PATH}\n"
                 f"Run: kci init")
    if not KCIDEV_PATH.exists():
        sys.exit(f"Error: kci-dev not found at {KCIDEV_PATH}\n"
                 f"Run: kci init")


def _validate_kernel(path: str) -> KernelSource:
    """Validate kernel source path or clone URL."""
    if path.endswith(".git") or path.startswith("git://"):
        clone_dir = HOME / Path(path).stem
        if not clone_dir.exists():
            subprocess.run(["git", "clone", "--depth=1", path, str(clone_dir)], check=True)
        path = str(clone_dir)
    p = Path(path)
    if not p.is_dir():
        sys.exit(f"Error: kernel source not found: {path}")
    makefile = p / "Makefile"
    if not makefile.exists() or "VERSION" not in makefile.read_text()[:500]:
        sys.exit(f"Error: {path} does not look like a kernel tree")
    return KernelSource(path=p)


def _config_hash(config_path: Path) -> str:
    """SHA256 of config file for incremental build detection."""
    return hashlib.sha256(config_path.read_bytes()).hexdigest()[:12]


# --- Commands ---

def cmd_init(args: argparse.Namespace) -> None:
    """One-time setup."""
    venv = HOME / "venv-virtme"
    run = lambda cmd: subprocess.run(cmd, shell=True, check=True)

    print("=== Setting up venv ===")
    run(f"python3 -m venv {venv}")
    run(f"{venv}/bin/pip3 install --upgrade --force-reinstall git+https://github.com/arighi/virtme-ng.git")
    run(f"{venv}/bin/pip3 install --upgrade --force-reinstall git+https://github.com/kernelci/kci-dev.git")
    run(f"{venv}/bin/pip3 install jsonschema scapy")

    print("=== Installing system deps ===")
    run("sudo apt-get update -qq")
    run("sudo apt-get install -y -qq "
        "iproute2 iptables nftables ethtool jq socat ncat traceroute arping "
        "netsniff-ng tcpdump iputils-ping linux-tools-common ndisc6 libmnl-dev pkg-config "
        "clang lld llvm make bc flex bison libelf-dev libssl-dev libcap-dev "
        "libdw-dev libnuma-dev rsync iperf3 qemu-system-x86")

    print("=== Configuring QEMU bridge ===")
    run("sudo mkdir -p /etc/qemu")
    run("echo 'allow all' | sudo tee /etc/qemu/bridge.conf")
    run("sudo chmod 0644 /etc/qemu/bridge.conf")
    subprocess.run("sudo chmod u+s /usr/lib/qemu/qemu-bridge-helper", shell=True, check=False)

    print("=== Cloning kvm-unit-tests ===")
    if not KVM_UNIT_TESTS_DIR.exists():
        run(f"git clone https://gitlab.com/kvm-unit-tests/kvm-unit-tests.git {KVM_UNIT_TESTS_DIR}")
    run(f"cd {KVM_UNIT_TESTS_DIR} && ./configure --arch=x86_64 && make -j{os.cpu_count()}")

    print("\ninit done.")


def cmd_build(args: argparse.Namespace) -> None:
    """Build kernel + kselftest."""
    _check_binaries()
    kernel = _validate_kernel(args.kernel)
    kconfig = KernelConfig(source=args.config)
    config_path = resolve_config(kconfig, arch=getattr(args, "arch", "x86_64"))
    validate_config(config_path)

    llvm = "LLVM=1" if "LLVM=1" in (args.vars or []) else ""

    # Incremental build: skip --force if config unchanged
    config_hash_file = kernel.path / ".kci-config-hash"
    current_hash = _config_hash(config_path)
    force = True
    if config_hash_file.exists() and config_hash_file.read_text().strip() == current_hash:
        print("Config unchanged, using incremental build (no --force)")
        force = False

    print(f"=== Building kernel in {kernel.path} ===")
    cmd = [str(VNG_PATH), "--build"]
    if force:
        cmd.append("--force")
    cmd += [
        "--config", str(config_path),
        "--configitem", "CONFIG_KUNIT=y",
        "--configitem", "CONFIG_KUNIT_ALL_TESTS=y",
        "--configitem", "CONFIG_KUNIT_TEST=y",
        "--configitem", 'CONFIG_CMDLINE="earlyprintk=serial net.ifnames=0"',
        "--jobs", str(args.jobs),
    ]
    if llvm:
        cmd.append("LLVM=1")
    subprocess.run(cmd, cwd=kernel.path, check=True)
    config_hash_file.write_text(current_hash)

    print(f"=== Building kselftest ({args.targets}) ===")
    subprocess.run(f"make -j{args.jobs} headers", shell=True, cwd=kernel.path, check=True)
    subprocess.run(
        f'make -C tools/testing/selftests TARGETS="{args.targets}" '
        f"install INSTALL_PATH={kernel.path}/kselftest_install",
        shell=True, cwd=kernel.path, check=True,
    )

    # Validate install
    install_dir = kernel.path / "kselftest_install"
    run_script = install_dir / "run_kselftest.sh"
    if not run_script.exists():
        sys.exit(f"Error: kselftest install failed — {run_script} not found")
    test_dirs = [d for d in install_dir.iterdir() if d.is_dir()]
    print(f"kselftest installed: {len(test_dirs)} target dirs")

    print("\nbuild done.")


def cmd_run(args: argparse.Namespace) -> None:
    """Run tests."""
    _check_binaries()
    kernel = _validate_kernel(args.kernel)
    config = RunConfig(
        jobs=args.jobs,
        targets=args.targets,
        arch=getattr(args, "arch", "x86_64"),
    )
    runner = VirtmeRunner(VNG_PATH)
    suite = getattr(args, "suite", None)
    filter_pattern = getattr(args, "filter", None)

    results: list[TestResults] = []

    if suite is None or suite == "kunit":
        results.append(run_kunit(runner, kernel, config))
    if suite is None or suite == "kselftest":
        results.append(run_kselftest(runner, kernel, config, filter_pattern=filter_pattern))
    if suite is None or suite == "kvm-unit-tests":
        results.append(run_kvm_unit_tests(KVM_UNIT_TESTS_DIR, kernel))

    if suite is None:
        upstream = fetch_upstream_failures(KCIDEV_PATH, kernel, arch=config.arch)
        regressions = detect_regressions(kernel, upstream)
        print_summary(results, regressions, len(upstream))
    else:
        for r in results:
            print(f"\n{r.suite}: {r.passed} pass, {r.failed} fail, {r.skipped} skip")


def cmd_report(args: argparse.Namespace) -> None:
    """Generate report."""
    _check_binaries()
    kernel = _validate_kernel(args.kernel)
    from .runner import _parse_kunit_results, _parse_kselftest_results

    results: list[TestResults] = []
    kunit_file = kernel.results_dir / "kunit.txt"
    kselftest_file = kernel.results_dir / "kselftest.txt"
    kvm_file = kernel.results_dir / "kvm-unit-tests.txt"

    if kunit_file.exists():
        results.append(_parse_kunit_results(kunit_file))
    if kselftest_file.exists():
        results.append(_parse_kselftest_results(kselftest_file))
    if kvm_file.exists():
        import re
        text = kvm_file.read_text()
        lines = text.splitlines()
        failed_names = [l for l in lines if l.startswith("FAIL")]
        results.append(TestResults(
            suite="kvm-unit-tests",
            passed=sum(1 for l in lines if l.startswith("PASS")),
            failed=sum(1 for l in lines if l.startswith("FAIL")),
            skipped=sum(1 for l in lines if l.startswith("SKIP")),
            output_file=kvm_file,
            failed_tests=failed_names,
        ))

    arch = getattr(args, "arch", "x86_64")
    upstream = fetch_upstream_failures(KCIDEV_PATH, kernel, arch=arch)
    regressions = detect_regressions(kernel, upstream)

    # JSON output
    if getattr(args, "json", False):
        report_data = {
            "date": datetime.now().isoformat(),
            "kernel": str(kernel.path),
            "arch": arch,
            "results": [
                {"suite": r.suite, "passed": r.passed, "failed": r.failed,
                 "skipped": r.skipped, "total": r.total, "failed_tests": r.failed_tests}
                for r in results
            ],
            "upstream_failures": len(upstream),
            "regressions": regressions,
        }
        json_path = Path("/tmp/kci-report.json")
        json_path.write_text(json.dumps(report_data, indent=2) + "\n")
        print(json.dumps(report_data, indent=2))
        print(f"\nJSON report: {json_path}")
        return

    # Markdown report
    report_path = Path("/tmp/kci-report.md")
    md = [
        "# KCI Test Report",
        "",
        f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}  ",
        f"**Kernel:** `{kernel.path}`  ",
        f"**Arch:** {arch}  ",
        "",
        "## Results",
        "",
        "| Suite | Pass | Fail | Skip | Total |",
        "|-------|------|------|------|-------|",
    ]
    for r in results:
        md.append(f"| {r.suite} | {r.passed} | {r.failed} | {r.skipped} | {r.total} |")

    # Failed test names
    all_failed = []
    for r in results:
        all_failed.extend(r.failed_tests)
    if all_failed:
        md += ["", "## Failed Tests", ""]
        for t in all_failed[:100]:
            md.append(f"- `{t}`")

    md += [
        "", "## Upstream Comparison", "",
        f"- **Upstream known failures:** {len(upstream)}",
        f"- **Local-only failures:** {len(regressions)}",
        "",
    ]
    if regressions:
        md.append("### ⚠️ Potential Regressions (NOT seen upstream)")
        md.append("")
        for f in regressions[:50]:
            md.append(f"- `{f}`")
    else:
        md.append("### ✅ No new regressions")
        md.append("")
        md.append("All local failures match known upstream failures.")

    md += ["", "---", "*Generated by kci*"]
    report_path.write_text("\n".join(md) + "\n")

    print_summary(results, regressions, len(upstream))
    print(f"\nReport written to: {report_path}")


def cmd_submit(args: argparse.Namespace) -> None:
    """Submit results to KCIDB (requires token)."""
    _check_binaries()
    kernel = _validate_kernel(args.kernel)
    results_dir = kernel.results_dir

    if not results_dir.exists() or not any(results_dir.iterdir()):
        sys.exit("Error: no test results found. Run tests first: kci run")

    print("Submitting results to KernelCI...")
    # TODO: implement once KCIDB token is available
    # kci-dev submit --results <results_dir>
    print("Note: kci submit requires a KCIDB token.")
    print("Request one at: https://github.com/kernelci/kernelci-core/issues/new?template=kernelci-api-tokens.md")
    print(f"Results directory: {results_dir}")


def main() -> None:
    """Entry point."""
    parser = argparse.ArgumentParser(prog="kci", description="Local kernel CI tool with KernelCI integration")
    sub = parser.add_subparsers(dest="command")

    # init
    sub.add_parser("init", help="One-time setup: venv, tools, kvm-unit-tests")

    # build
    p_build = sub.add_parser("build", help="Build kernel + kselftest")
    p_build.add_argument("-c", "--config", default="syzkaller", help="Kernel config (path, URL, or 'syzkaller')")
    p_build.add_argument("-k", "--kernel", default=str(HOME / "net"), help="Kernel source (path or git URL)")
    p_build.add_argument("-t", "--targets", default="net bpf mm cgroup timers net/forwarding")
    p_build.add_argument("-j", "--jobs", type=int, default=os.cpu_count())
    p_build.add_argument("--arch", default="x86_64", help="Architecture (default: x86_64)")
    p_build.add_argument("vars", nargs="*", help="Make variables (e.g. LLVM=1)")

    # run
    p_run = sub.add_parser("run", help="Run tests")
    p_run.add_argument("-k", "--kernel", default=str(HOME / "net"))
    p_run.add_argument("-t", "--targets", default="net bpf mm cgroup timers net/forwarding")
    p_run.add_argument("-j", "--jobs", type=int, default=os.cpu_count())
    p_run.add_argument("-f", "--filter", help="Filter kselftest (e.g. 'net:tls')")
    p_run.add_argument("--arch", default="x86_64", help="Architecture (default: x86_64)")
    p_run.add_argument("suite", nargs="?", choices=["kunit", "kselftest", "kvm-unit-tests"],
                       help="Run specific test suite")

    # report
    p_report = sub.add_parser("report", help="Compare results with KernelCI upstream")
    p_report.add_argument("-k", "--kernel", default=str(HOME / "net"))
    p_report.add_argument("--json", action="store_true", help="Output JSON instead of Markdown")
    p_report.add_argument("--arch", default="x86_64")

    # submit
    p_submit = sub.add_parser("submit", help="Submit results to KCIDB (requires token)")
    p_submit.add_argument("-k", "--kernel", default=str(HOME / "net"))

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    dispatch = {
        "init": cmd_init,
        "build": cmd_build,
        "run": cmd_run,
        "report": cmd_report,
        "submit": cmd_submit,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
