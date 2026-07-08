"""Kernel config fetcher — downloads latest syzkaller config."""

import html
import re
import subprocess
import urllib.request
from pathlib import Path

from .models import KernelConfig

SYZKALLER_BASE_URL = "https://syzkaller.appspot.com"
SYZKALLER_UPSTREAM_URL = f"{SYZKALLER_BASE_URL}/upstream"

# Flavors map to syzbot instances tuned for each sanitizer. Notably the
# generic (KASAN) config does not link with KMSAN instrumentation added on
# top ("kernel image bigger than KERNEL_IMAGE_SIZE"); the dedicated KMSAN
# config is sized for it and built daily by syzbot.
SYZKALLER_MANAGERS = {
    "x86_64": {
        "syzkaller": "ci-qemu-gce-upstream-auto",
        "syzkaller-kcsan": "ci2-upstream-kcsan-gce",
        "syzkaller-kmsan": "ci-upstream-kmsan-gce-root",
    },
    "arm64": {
        "syzkaller": "ci-upstream-gce-arm64",
    },
}


def fetch_latest_syzkaller_config(dest: Path = Path("/tmp/kci-kernel.config"),
                                  arch: str = "x86_64",
                                  flavor: str = "syzkaller") -> Path:
    """Scrape syzkaller dashboard for the latest upstream kernel config."""
    managers = SYZKALLER_MANAGERS.get(arch)
    if not managers:
        raise ValueError(f"No syzkaller config for arch: {arch}. Available: {list(SYZKALLER_MANAGERS.keys())}")
    manager = managers.get(flavor)
    if not manager:
        raise ValueError(f"No syzkaller flavor '{flavor}' for {arch}. Available: {list(managers.keys())}")

    print(f"Fetching latest syzkaller config ({manager})...")
    with urllib.request.urlopen(SYZKALLER_UPSTREAM_URL) as response:
        html_content = response.read().decode("utf-8")

    pattern = re.compile(
        rf"{manager}.*?href=\"([^\"]*?tag=KernelConfig[^\"]*?)\"",
        re.DOTALL,
    )
    match = pattern.search(html_content)
    if not match:
        raise RuntimeError(f"Could not find config link for {manager}")

    config_url = f"{SYZKALLER_BASE_URL}{html.unescape(match.group(1))}"
    print(f"Downloading: {config_url}")
    urllib.request.urlretrieve(config_url, str(dest))
    return dest


def resolve_config(config: KernelConfig, arch: str = "x86_64") -> Path:
    """Resolve a KernelConfig to a local file path."""
    if config.source.startswith("syzkaller"):
        return fetch_latest_syzkaller_config(config.resolved_path, arch=arch,
                                             flavor=config.source)
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
