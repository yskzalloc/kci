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
kci report -k ~/linux                 # compare with upstream, write /tmp/kci-report.md
```

## Config

By default, fetches the latest syzkaller upstream kernel config. Override with:

```sh
kci build -c /path/to/config -k ~/linux
kci build -c https://example.com/config -k ~/linux
```

## License

GPL-2.0
