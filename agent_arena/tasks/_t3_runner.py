"""Hidden-test runner for T3 (subprocess-isolated).

Usage:
    python -I _t3_runner.py <solution_dir> <tests_json_path>

``tests_json`` is written by the harness and contains a list of test-module
sources (plain Python text, one per module).  Each module is exec'd into a
fresh ``types.ModuleType`` with ``solution`` and a minimal pytest-compatible
``raises`` helper injected, every ``test_*`` function is executed, and a
``SUMMARY <p> passed, <f> failed, <s> error`` line plus failure details are
printed.
"""

import json
import sys
import traceback
from types import ModuleType


class _Raises:
    """Minimal pytest-style ``with raises(ExcType):`` context manager."""

    def __init__(self, exc_type):
        if not isinstance(exc_type, type) or not issubclass(exc_type, BaseException):
            raise TypeError(f"raises() needs an exception class, got {exc_type!r}")
        self.exc_type = exc_type

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError(
                f"DID NOT RAISE {self.exc_type.__name__}"
            )
        return issubclass(exc_type, self.exc_type)


def run_suite(solution_code: str, modules):
    # inject the candidate solution as module "solution"
    solution = ModuleType("solution")
    solution.__dict__["raises"] = _Raises
    sys.modules["solution"] = solution
    # minimal pytest stand-in so test sources can `from pytest_shim import raises`
    shim = ModuleType("pytest_shim")
    shim.raises = _Raises
    sys.modules["pytest_shim"] = shim
    try:
        exec(compile(solution_code, "solution.py", "exec"), solution.__dict__)
    except Exception:
        print("SOLUTION_IMPORT_ERROR")
        print(traceback.format_exc(limit=4))
        return

    results = []
    for idx, src in enumerate(modules):
        mod = ModuleType(f"hidden_tests_{idx}")
        mod.__dict__["solution"] = solution
        mod.__dict__["raises"] = _Raises
        try:
            exec(compile(src, f"hidden_tests_{idx}.py", "exec"), mod.__dict__)
        except Exception:
            results.append((f"module_{idx}", "<import>", False, traceback.format_exc(limit=3)))
            continue
        for name in sorted(dir(mod)):
            if not name.startswith("test_"):
                continue
            fn = getattr(mod, name)
            if not callable(fn):
                continue
            try:
                fn()
                results.append((f"module_{idx}", name, True, ""))
            except Exception:
                results.append((f"module_{idx}", name, False, traceback.format_exc(limit=3)))

    passed = sum(1 for r in results if r[2] is True)
    failed = sum(1 for r in results if r[2] is False)
    for modname, name, ok, detail in results:
        if not ok:
            print(f"FAILED {modname}::{name}")
            print(detail)
    print(f"SUMMARY {passed} passed, {failed} failed, 0 error")


def main() -> None:
    solution_dir, tests_json = sys.argv[1], sys.argv[2]
    with open(f"{solution_dir}/solution.py", "r", encoding="utf-8") as f:
        solution_code = f.read()
    with open(tests_json, "r", encoding="utf-8") as f:
        modules = json.load(f)
    run_suite(solution_code, modules)


if __name__ == "__main__":
    main()
