"""Mini test runner for the T1 virtual repo (subprocess-isolated).

Usage:
    python -I _t1_runner.py <repo_dir>            # run tests
    python -I _t1_runner.py <repo_dir> --check-hash  # verify hashes.py

Runs every ``test_*`` function in ``tests/test_*.py`` and prints a final
``SUMMARY <p> passed, <f> failed, <s> skipped`` line.  Skipped tests use the
standard ``unittest.skip`` decorator attribute.
"""

import importlib.util
import os
import sys
import traceback


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    repo = os.path.abspath(sys.argv[1])
    sys.path.insert(0, repo)

    if "--check-hash" in sys.argv:
        try:
            h = load_module(os.path.join(repo, "hashes.py"), "hashes_check")
            expected = getattr(h, "EXPECTED_INVOICE_HASH", None)
            computed = h.compute_hash("billing-v2:in_7")
        except Exception:
            print("HASH_ERROR")
            print(traceback.format_exc(limit=3))
            return
        print("HASH_OK" if expected == computed else "HASH_BAD")
        print(f"expected={expected!r} computed={computed!r}")
        return

    results = []  # (file, name, True|False|"skip", detail)
    tests_dir = os.path.join(repo, "tests")
    if os.path.isdir(tests_dir):
        for fname in sorted(os.listdir(tests_dir)):
            if not (fname.startswith("test_") and fname.endswith(".py")):
                continue
            path = os.path.join(tests_dir, fname)
            try:
                mod = load_module(path, f"t1tests_{fname[:-3]}")
            except Exception:
                results.append((fname, "<module>", False, traceback.format_exc(limit=3)))
                continue
            for attr in sorted(dir(mod)):
                if not attr.startswith("test_"):
                    continue
                fn = getattr(mod, attr)
                if not callable(fn):
                    continue
                if getattr(fn, "__unittest_skip__", False):
                    results.append((fname, attr, "skip", getattr(fn, "__unittest_skip_why__", "")))
                    continue
                try:
                    fn()
                    results.append((fname, attr, True, ""))
                except Exception:
                    results.append((fname, attr, False, traceback.format_exc(limit=3)))

    passed = sum(1 for r in results if r[2] is True)
    failed = sum(1 for r in results if r[2] is False)
    skipped = sum(1 for r in results if r[2] == "skip")
    for fname, name, ok, detail in results:
        if ok is False:
            print(f"FAILED {fname}::{name}")
            print(detail)
        elif ok == "skip":
            print(f"SKIPPED {fname}::{name} {detail}")
    print(f"SUMMARY {passed} passed, {failed} failed, {skipped} skipped")


if __name__ == "__main__":
    main()
