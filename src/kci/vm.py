"""VM runner implementations using Protocol-based structural subtyping."""

from __future__ import annotations

import subprocess
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
            f"timeout {timeout} {self.vng} --rw "
            f"--memory {config.memory} --cpus {config.jobs} "
            f"--append 'panic_on_warn=0' --network {network}"
        )
        if user:
            cmd += f" --user {user}"
        cmd += f' --exec "{exec_cmd}"'
        return subprocess.run(cmd, shell=True, cwd=kernel.path, check=False,
                              capture_output=True, text=True)
