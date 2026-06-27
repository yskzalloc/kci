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
from .runner import run_kunit, run_kselftest, run_kvm_unit_tests, run_stress
from .vm import VirtmeRunner

HOME = Path.home()
VNG_PATH = HOME / "venv-virtme" / "bin" / "vng"
KCIDEV_PATH = HOME / "venv-virtme" / "bin" / "kci-dev"
KVM_UNIT_TESTS_DIR = HOME / "kvm-unit-tests"


def _check_binaries() -> None:
    """Check required binaries exist."""
    if not VNG_PATH.exists():
        sys.exit(f"Error: vng not found at {VNG_PATH}\nRun: kci init")
    if not KCIDEV_PATH.exists():
        sys.exit(f"Error: kci-dev not found at {KCIDEV_PATH}\nRun: kci init")


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


def _get_kernel_version(kernel: KernelSource) -> str:
    """Get kernel version from Makefile and git commit."""
    makefile = kernel.path / "Makefile"
    version = patchlevel = sublevel = ""
    for line in makefile.read_text().splitlines()[:10]:
        if line.startswith("VERSION"):
            version = line.split("=")[1].strip()
        elif line.startswith("PATCHLEVEL"):
            patchlevel = line.split("=")[1].strip()
        elif line.startswith("SUBLEVEL"):
            sublevel = line.split("=")[1].strip()
    ver = f"{version}.{patchlevel}.{sublevel}"
    try:
        commit = subprocess.run(
            ["git", "-C", str(kernel.path), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True
        ).stdout.strip()
        ver += f" ({commit})"
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return ver


def _get_kci_token() -> str | None:
    """Get KCI token from env or config file."""
    token = os.environ.get("KCI_TOKEN")
    if token:
        return token
    config_file = Path.home() / ".config" / "kci-dev" / "kci-dev.toml"
    if config_file.exists():
        for line in config_file.read_text().splitlines():
            if "token" in line and "=" in line:
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


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

    print("=== Cloning and building stress-ng ===")
    stress_ng_dir = HOME / "stress-ng"
    if not stress_ng_dir.exists():
        run(f"git clone https://github.com/ColinIanKing/stress-ng.git {stress_ng_dir}")
    run(f"make -j{os.cpu_count()} -C {stress_ng_dir}")

    print("\ninit done.")


def cmd_build(args: argparse.Namespace) -> None:
    """Build kernel + kselftest."""
    _check_binaries()
    kernel = _validate_kernel(args.kernel)
    kconfig = KernelConfig(source=args.config)
    config_path = resolve_config(kconfig, arch=getattr(args, "arch", "x86_64"))
    validate_config(config_path)

    llvm = "LLVM=1" if "LLVM=1" in (args.vars or []) else ""

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
        "--configitem", 'CONFIG_CMDLINE="earlyprintk=serial net.ifnames=0 panic_on_warn=0"',
        "--jobs", str(args.jobs),
    ]
    if llvm:
        cmd.append("LLVM=1")
    subprocess.run(cmd, cwd=kernel.path, check=True)
    config_hash_file.write_text(current_hash)

    # Merge kselftest config requirements (enables configs tests need)
    print("=== Merging kselftest config ===")
    subprocess.run(f"make -j{args.jobs} kselftest-merge", shell=True, cwd=kernel.path, check=False)

    print(f"=== Building kselftest ({args.targets}) ===")
    subprocess.run(f"make -j{args.jobs} headers", shell=True, cwd=kernel.path, check=True)
    skip = getattr(args, "skip_targets", "")
    if args.targets == "all":
        # Full bundle: build + install everything
        install_cmd = f"make -j{args.jobs} kselftest-install INSTALL_PATH={kernel.path}/kselftest_install"
        if skip:
            install_cmd = f'make -j{args.jobs} SKIP_TARGETS="{skip}" kselftest-install INSTALL_PATH={kernel.path}/kselftest_install'
    else:
        install_cmd = (
            f'make -C tools/testing/selftests TARGETS="{args.targets}" '
            f"install INSTALL_PATH={kernel.path}/kselftest_install"
        )
    subprocess.run(install_cmd, shell=True, cwd=kernel.path, check=True)

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
        retry=getattr(args, "retry", 0),
    )
    runner = VirtmeRunner(VNG_PATH)
    suite = getattr(args, "suite", None)
    filter_pattern = getattr(args, "filter", None)

    results: list[TestResults] = []

    if suite is None or suite == "kunit":
        results.append(run_kunit(runner, kernel, config))
    if suite is None or suite == "kselftest":
        results.append(run_kselftest(runner, kernel, config, filter_pattern=filter_pattern))
    if suite is None or suite == "stress":
        results.append(run_stress(runner, kernel, config))
    if suite is None or suite == "kvm-unit-tests":
        results.append(run_kvm_unit_tests(KVM_UNIT_TESTS_DIR, kernel))

    if suite is None:
        upstream = fetch_upstream_failures(KCIDEV_PATH, kernel, arch=config.arch)
        regressions = detect_regressions(kernel, upstream)
        print_summary(results, regressions, len(upstream))
    else:
        for r in results:
            print(f"\n{r.suite}: {r.passed} pass, {r.failed} fail, {r.skipped} skip")
            if r.flaky_tests:
                print(f"  flaky: {len(r.flaky_tests)}")


def cmd_report(args: argparse.Namespace) -> None:
    """Generate report."""
    _check_binaries()
    kernel = _validate_kernel(args.kernel)
    from .runner import _parse_kunit_results, _parse_kselftest_results

    kernel_version = _get_kernel_version(kernel)

    results: list[TestResults] = []
    kunit_file = kernel.results_dir / "kunit.txt"
    kselftest_file = kernel.results_dir / "kselftest.txt"
    kvm_file = kernel.results_dir / "kvm-unit-tests.txt"

    if kunit_file.exists():
        results.append(_parse_kunit_results(kunit_file))
    if kselftest_file.exists():
        results.append(_parse_kselftest_results(kselftest_file))
    if kvm_file.exists():
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
            "kernel_version": kernel_version,
            "arch": arch,
            "results": [
                {"suite": r.suite, "passed": r.passed, "failed": r.failed,
                 "skipped": r.skipped, "total": r.total, "failed_tests": r.failed_tests,
                 "bugs": r.bugs}
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
        f"**Version:** {kernel_version}  ",
        f"**Arch:** {arch}  ",
        "",
        "## Results",
        "",
        "| Suite | Pass | Fail | Skip | Total |",
        "|-------|------|------|------|-------|",
    ]
    for r in results:
        md.append(f"| {r.suite} | {r.passed} | {r.failed} | {r.skipped} | {r.total} |")

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

    # GitHub notification on regression
    if regressions:
        _notify_github(regressions, kernel_version, arch)


def _notify_github(regressions: list[str], kernel_version: str, arch: str) -> None:
    """Create GitHub issue if regressions found and GITHUB_REPOSITORY is set."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        return
    title = f"Kernel regression detected: {kernel_version} ({arch})"
    body = f"## Regressions found in {kernel_version}\\n\\n"
    for r in regressions[:20]:
        body += f"- `{r}`\\n"
    try:
        subprocess.run(
            ["gh", "api", f"/repos/{repo}/issues", "-f", f"title={title}", "-f", f"body={body}"],
            check=True, capture_output=True,
        )
        print(f"  GitHub issue created on {repo}")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"  GitHub notification skipped: {e}")


def cmd_submit(args: argparse.Namespace) -> None:
    """Submit results to KCIDB."""
    _check_binaries()
    kernel = _validate_kernel(args.kernel)
    results_dir = kernel.results_dir

    if not results_dir.exists() or not any(results_dir.iterdir()):
        sys.exit("Error: no test results found. Run tests first: kci run")

    token = _get_kci_token()
    if not token:
        sys.exit("Error: No KCI token found. Set KCI_TOKEN env var or configure "
                 "~/.config/kci-dev/kci-dev.toml\n"
                 "Request one at: https://github.com/kernelci/kernelci-core/issues/new?template=kernelci-api-tokens.md")

    kernel_version = _get_kernel_version(kernel)
    print(f"Submitting results for {kernel_version}...")

    # Prepare KCIDB-compatible JSON
    from .runner import _parse_kunit_results, _parse_kselftest_results
    tests = []
    kselftest_file = results_dir / "kselftest.txt"
    kunit_file = results_dir / "kunit.txt"

    for fpath, suite in [(kunit_file, "kunit"), (kselftest_file, "kselftest")]:
        if fpath.exists():
            parse_fn = _parse_kunit_results if suite == "kunit" else _parse_kselftest_results
            r = parse_fn(fpath)
            tests.append({
                "id": f"kci:{suite}:{kernel_version}",
                "build_id": f"kci:build:{kernel_version}",
                "path": suite,
                "status": "PASS" if r.failed == 0 else "FAIL",
                "start_time": datetime.now().isoformat(),
            })

    submission = {"version": {"major": 4, "minor": 3}, "tests": tests}
    json_path = results_dir / "kcidb-submission.json"
    json_path.write_text(json.dumps(submission, indent=2) + "\n")

    # Try kci-dev submit
    result = subprocess.run(
        [str(KCIDEV_PATH), "submit", "--token", token, "--json", str(json_path)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode == 0:
        print("Results submitted successfully.")
    else:
        print(f"kci-dev submit output: {result.stdout}{result.stderr}")
        print(f"Submission JSON saved to: {json_path}")


def cmd_notify(args: argparse.Namespace) -> None:
    """Create GitHub issue for regressions."""
    _check_binaries()
    kernel = _validate_kernel(args.kernel)
    arch = getattr(args, "arch", "x86_64")

    upstream = fetch_upstream_failures(KCIDEV_PATH, kernel, arch=arch)
    regressions = detect_regressions(kernel, upstream)

    if not regressions:
        print("No regressions found. No notification sent.")
        return

    kernel_version = _get_kernel_version(kernel)
    _notify_github(regressions, kernel_version, arch)
    if not os.environ.get("GITHUB_REPOSITORY"):
        print("Set GITHUB_REPOSITORY env var to enable GitHub notifications.")


def cmd_bisect(args: argparse.Namespace) -> None:
    """Bisect to find commit that introduced a test failure."""
    _check_binaries()
    kernel = _validate_kernel(args.kernel)

    good = args.good
    bad = args.bad
    test = args.test

    print(f"Bisecting {kernel.path}: good={good} bad={bad} test={test}")

    # Create a test script for git bisect run
    script = kernel.path / ".kci-bisect-test.sh"
    script.write_text(f"""#!/bin/bash
set -e
# Build
{VNG_PATH} --build --force --jobs {os.cpu_count()} 2>/dev/null
# Run the specific test
RESULT=$({VNG_PATH} --rw --memory 4G --cpus {os.cpu_count()} --user root \\
    --exec "cd kselftest_install && ./run_kselftest.sh -t {test} 2>&1" 2>/dev/null)
if echo "$RESULT" | grep -q "^not ok"; then
    exit 1
fi
exit 0
""")
    script.chmod(0o755)

    # Run git bisect
    subprocess.run(["git", "-C", str(kernel.path), "bisect", "start"], check=True)
    subprocess.run(["git", "-C", str(kernel.path), "bisect", "bad", bad], check=True)
    subprocess.run(["git", "-C", str(kernel.path), "bisect", "good", good], check=True)

    result = subprocess.run(
        ["git", "-C", str(kernel.path), "bisect", "run", str(script)],
        check=False,
    )

    # Show result
    subprocess.run(["git", "-C", str(kernel.path), "bisect", "log"], check=False)
    subprocess.run(["git", "-C", str(kernel.path), "bisect", "reset"], check=False)
    script.unlink(missing_ok=True)

    sys.exit(result.returncode)


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
    p_build.add_argument("-t", "--targets", default="all",
                         help="Kselftest targets (or 'all' for full suite)")
    p_build.add_argument("-s", "--skip", default="arm64 powerpc sparc64 riscv ia64",
                         dest="skip_targets",
                         help="Skip these targets (default: non-x86 archs)")
    p_build.add_argument("-j", "--jobs", type=int, default=os.cpu_count())
    p_build.add_argument("--arch", default="x86_64", help="Architecture (default: x86_64)")
    p_build.add_argument("vars", nargs="*", help="Make variables (e.g. LLVM=1)")

    # run
    p_run = sub.add_parser("run", help="Run tests")
    p_run.add_argument("-k", "--kernel", default=str(HOME / "net"))
    p_run.add_argument("-t", "--targets", default="all")
    p_run.add_argument("-j", "--jobs", type=int, default=os.cpu_count())
    p_run.add_argument("-f", "--filter", help="Filter kselftest (e.g. 'net:tls')")
    p_run.add_argument("--arch", default="x86_64", help="Architecture (default: x86_64)")
    p_run.add_argument("--retry", type=int, default=0, help="Retry failed tests N times")
    p_run.add_argument("suite", nargs="?", choices=["kunit", "kselftest", "kvm-unit-tests", "stress"],
                       help="Run specific test suite")

    # report
    p_report = sub.add_parser("report", help="Compare results with KernelCI upstream")
    p_report.add_argument("-k", "--kernel", default=str(HOME / "net"))
    p_report.add_argument("--json", action="store_true", help="Output JSON instead of Markdown")
    p_report.add_argument("--arch", default="x86_64")

    # submit
    p_submit = sub.add_parser("submit", help="Submit results to KCIDB")
    p_submit.add_argument("-k", "--kernel", default=str(HOME / "net"))

    # notify
    p_notify = sub.add_parser("notify", help="GitHub notification on regression")
    p_notify.add_argument("-k", "--kernel", default=str(HOME / "net"))
    p_notify.add_argument("--arch", default="x86_64")

    # bisect
    p_bisect = sub.add_parser("bisect", help="Bisect to find regression commit")
    p_bisect.add_argument("-k", "--kernel", default=str(HOME / "net"))
    p_bisect.add_argument("--good", required=True, help="Known good commit")
    p_bisect.add_argument("--bad", required=True, help="Known bad commit")
    p_bisect.add_argument("--test", required=True, help="Test to run (e.g. 'net:tls')")

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
        "notify": cmd_notify,
        "bisect": cmd_bisect,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
