# kci

Local kernel CI tool with KernelCI integration.

Builds a kernel with LLVM, runs kunit/kselftest/kvm-unit-tests in QEMU via virtme-ng, and compares results against the KernelCI upstream dashboard.

## Install

```sh
pip install -e .
```

## Usage

```sh
kci init                              # one-time setup
kci build -k ~/linux LLVM=1           # build kernel + kselftest
kci run -k ~/linux                    # run all tests
kci run kunit                         # run only kunit
kci run kselftest -t "net" -j 4       # run kselftest with specific targets
kci run kvm-unit-tests                # run kvm-unit-tests
kci run ksmbd                         # xfstests over cifs.ko against in-kernel ksmbd
kci report -k ~/linux                 # compare with upstream, write /tmp/kci-report.md
```

Test targets live one-per-file under `src/kci/run/` (`kunit.py`, `kselftest.py`,
`kvm_unit_tests.py`, `stress.py`, `ksmbd.py`); `kci run` just invokes them.
Helper scripts run inside the VM are in `src/kci/run/scripts/`.

### ksmbd (xfstests over cifs.ko)

The `ksmbd` target boots the built kernel (needs `CONFIG_SMB_SERVER=y` and
`CONFIG_CIFS=y`, e.g. `kci build -C CONFIG_SMB_SERVER=y -C CONFIG_CIFS=y ...`),
starts `ksmbd.mountd` inside the guest and runs xfstests against
`//127.0.0.1` shares. See `xfstesting-cifs.rst` for background. It expects
xfstests built at `~/xfstests-dev` (override with `KCI_XFSTESTS_DIR`) and
ksmbd-tools installed on the host. The default test list is storage-light to
fit GitHub free-tier runners (~14GB free disk); pass an explicit list with
`kci run ksmbd -f "generic/001 generic/002"`.

## Config

By default, fetches the latest syzkaller upstream kernel config. Override with:

```sh
kci build -c /path/to/config -k ~/linux
kci build -c https://example.com/config -k ~/linux
```

## License

GPL-2.0
