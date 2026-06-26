"""Data models for kci."""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class KernelConfig:
    """Kernel configuration source."""
    source: str  # local path, URL, or 'syzkaller'
    resolved_path: Path = field(default=Path("/tmp/kci-kernel.config"), init=False)

    def is_url(self) -> bool:
        return self.source.startswith("http://") or self.source.startswith("https://")


@dataclass
class KernelSource:
    """Kernel source tree."""
    path: Path
    results_dir: Path = field(init=False)

    def __post_init__(self):
        self.path = Path(self.path).resolve()
        self.results_dir = self.path / "test-results"


@dataclass
class RunConfig:
    """Runtime configuration for test execution."""
    jobs: int
    targets: str = "net bpf mm cgroup timers net/forwarding"
    memory: str = "4G"
    timeout_kunit: int = 1800
    timeout_kselftest: int = 7200
    arch: str = "x86_64"
    retry: int = 0


@dataclass
class TestResults:
    """Aggregated test results."""
    suite: str
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    output_file: Path | None = None
    failed_tests: list[str] = field(default_factory=list)
    bugs: list[str] = field(default_factory=list)
    flaky_tests: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.skipped
