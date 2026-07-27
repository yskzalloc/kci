"""Run targets — one file per target; this package invokes them.

Each module under kci/run/ implements a single test target with a run()
entry point (and parse_results() where output is re-parsed later).
This __init__ replaces the old runner.py: it only dispatches/re-exports.
"""

from . import ksmbd, kselftest, kunit, kvm_unit_tests, stress_ng

TARGETS = {
    "kunit": kunit,
    "kselftest": kselftest,
    "kvm-unit-tests": kvm_unit_tests,
    "stress-ng": stress_ng,
    "ksmbd": ksmbd,
}

run_kunit = kunit.run
run_kselftest = kselftest.run
run_kvm_unit_tests = kvm_unit_tests.run
run_stress_ng = stress_ng.run
run_ksmbd = ksmbd.run

# Kept under the old private names for existing callers (cli.py).
_parse_kunit_results = kunit.parse_results
_parse_kselftest_results = kselftest.parse_results
_parse_ksmbd_results = ksmbd.parse_results

__all__ = [
    "TARGETS",
    "run_kunit", "run_kselftest", "run_kvm_unit_tests", "run_stress_ng",
    "run_ksmbd",
]
