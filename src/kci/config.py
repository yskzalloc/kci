"""Kernel config fetcher — downloads latest syzkaller config."""

import html
import re
import subprocess
import urllib.request
from pathlib import Path

from .models import KernelConfig

SYZKALLER_BASE_URL = "https://syzkaller.appspot.com"
SYZKALLER_UPSTREAM_URL = f"{SYZKALLER_BASE_URL}/upstream"
TARGET_MANAGER = "ci-qemu-gce-upstream-auto"


def fetch_latest_syzkaller_config(dest: Path = Path("/tmp/kci-kernel.config")) -> Path:
    """Scrape syzkaller dashboard for the latest upstream kernel config."""
    print("Fetching latest syzkaller config...")
    with urllib.request.urlopen(SYZKALLER_UPSTREAM_URL) as response:
        html_content = response.read().decode("utf-8")

    pattern = re.compile(
        rf"{TARGET_MANAGER}.*?href=\"([^\"]*?tag=KernelConfig[^\"]*?)\"",
        re.DOTALL,
    )
    match = pattern.search(html_content)
    if not match:
        raise RuntimeError(f"Could not find config link for {TARGET_MANAGER}")

    config_url = f"{SYZKALLER_BASE_URL}{html.unescape(match.group(1))}"
    print(f"Downloading: {config_url}")
    urllib.request.urlretrieve(config_url, str(dest))
    return dest


def resolve_config(config: KernelConfig) -> Path:
    """Resolve a KernelConfig to a local file path."""
    if config.source == "syzkaller":
        return fetch_latest_syzkaller_config(config.resolved_path)
    elif config.is_url():
        print(f"Downloading config from {config.source}...")
        subprocess.run(
            ["wget", "-q", "-O", str(config.resolved_path), config.source],
            check=True,
        )
        return config.resolved_path
    else:
        p = Path(config.source)
        if not p.exists():
            raise FileNotFoundError(f"Config not found: {config.source}")
        return p.resolve()


def validate_config(path: Path) -> None:
    """Validate that a file looks like a kernel config."""
    if not any(l.startswith("CONFIG_") for l in path.read_text().splitlines()):
        raise ValueError(f"{path} does not look like a kernel config")
