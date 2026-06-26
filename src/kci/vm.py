"""VM runner implementations using Protocol-based structural subtyping."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Protocol

from .models import KernelSource, RunConfig


class VMRunner(Protocol):
    """Interface for VM runners (structural subtyping)."""

    def run(self, kernel: KernelSource, exec_cmd: str, config: RunConfig,
            user: str | None = None, network: str = "user",
            timeout: int = 7200) -> subprocess.CompletedProcess:
        ...


class VirtmeRunner:
    """Run tests using virtme-ng (QEMU wrapper)."""

    def __init__(self, vng_path: Path):
        self.vng = vng_path
        if not vng_path.exists():
            raise FileNotFoundError(
                f"vng not found at {vng_path}. Run: kci init")

    def run(self, kernel: KernelSource, exec_cmd: str, config: RunConfig,
            user: str | None = None, network: str = "user",
            timeout: int = 7200) -> subprocess.CompletedProcess:
        cmd = (
            f"timeout {timeout} {self.vng} --rw --verbose "
            f"--memory {config.memory} --cpus {config.jobs} "
            f"--append 'panic_on_warn=0' --network {network}"
        )
        if user:
            cmd += f" --user {user}"
        cmd += f' --exec "{exec_cmd}"'

        # Stream output live while capturing
        captured = []
        proc = subprocess.Popen(cmd, shell=True, cwd=kernel.path,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True)
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            captured.append(line)
        proc.wait()
        stdout = "".join(captured)
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout=stdout, stderr="")
