"""Regression test runner for the HLSL interpreter.

Reads the list of capture zips from ``Cases/regression_test_zip_files.csv`` and
runs each one through the full pipeline (headless — the mesh viewer is disabled).
A case PASSES when the run exits cleanly, its log contains no ``Error:`` lines
(per-component VS-vs-golden mismatches), and every golden row matched
(``Total PASSED rows: X/X``).

Usage:
    python run_regression.py            # run every zip listed in the CSV
    python run_regression.py --keep-logs  # keep per-case logs and temp configs

Exit code is non-zero if any case fails, so it can gate a change.
"""

import csv
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
CASES_DIR = os.path.join(ROOT, 'Cases')
CSV_PATH = os.path.join(CASES_DIR, 'regression_test_zip_files.csv')
LOG_DIR = os.path.join(CASES_DIR, 'regression_logs')

# Base config shared by every regression case. The mesh viewer is OFF so the
# run is fully headless; topology is intentionally omitted so each zip's
# pipeline_state.csv decides it.
BASE_CONFIG = {
    "log_file_path": "",          # filled per-case
    "data_path": "",              # filled per-case
    "early_z": True,
    "log_to_file": True,
    "printSyntaxTree": False,
    "print_interpreter_result": False,
    "print_sequence": 100,
    "float_tolerance": 0.005,
    "execute_count": -1,
    "max_workers": 1,
    "mesh_view_enabled": False,
    "log_file_mode": "w",
}

_PASSED_RE = re.compile(r"Total PASSED rows:\s*(\d+)\s*/\s*(\d+)")


def read_zip_list():
    """Return the list of zip filenames from the regression CSV (header skipped)."""
    if not os.path.exists(CSV_PATH):
        print(f"Error: regression list not found: {CSV_PATH}")
        sys.exit(1)
    names = []
    with open(CSV_PATH, 'r', encoding='utf-8-sig', newline='') as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            name = row[0].strip()
            if not name or name.lower() == 'filename':
                continue
            names.append(name)
    return names


def analyze_log(log_path):
    """Return (error_count, passed, total) parsed from a case log."""
    if not os.path.exists(log_path):
        return None, None, None
    error_count = 0
    passed = total = None
    with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            if line.lstrip().startswith('Error:'):
                error_count += 1
            m = _PASSED_RE.search(line)
            if m:
                passed, total = int(m.group(1)), int(m.group(2))
    return error_count, passed, total


def run_case(zip_name, keep):
    """Run one zip; return a result dict."""
    stem = os.path.splitext(zip_name)[0]
    os.makedirs(LOG_DIR, exist_ok=True)
    log_rel = os.path.join('regression_logs', stem + '.log')
    log_abs = os.path.join(LOG_DIR, stem + '.log')

    config = dict(BASE_CONFIG)
    config['data_path'] = './Cases/' + zip_name
    config['log_file_path'] = log_rel
    cfg_path = os.path.join(CASES_DIR, '.regression_tmp_' + stem + '.json')
    with open(cfg_path, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4)

    result = {'name': zip_name, 'status': 'FAIL', 'detail': ''}
    try:
        proc = subprocess.run(
            [sys.executable, 'render.py', os.path.relpath(cfg_path, ROOT)],
            cwd=ROOT, capture_output=True, text=True, timeout=600,
        )
        error_count, passed, total = analyze_log(log_abs)
        if proc.returncode != 0:
            result['detail'] = f"render.py exit={proc.returncode}: {(proc.stdout + proc.stderr).strip()[-200:]}"
        elif error_count is None:
            result['detail'] = "no log produced"
        elif total is None:
            result['detail'] = "no VS-vs-golden comparison in log"
        elif error_count > 0:
            result['detail'] = f"{error_count} Error: line(s); passed {passed}/{total}"
        elif passed != total:
            result['detail'] = f"passed {passed}/{total}"
        else:
            result['status'] = 'PASS'
            result['detail'] = f"passed {passed}/{total}"
    except subprocess.TimeoutExpired:
        result['detail'] = "timeout (>600s)"
    finally:
        if not keep and os.path.exists(cfg_path):
            os.remove(cfg_path)
    return result


def main():
    keep = '--keep-logs' in sys.argv[1:]
    zips = read_zip_list()
    if not zips:
        print("No regression zips listed in the CSV — nothing to run.")
        return 0

    print(f"Running {len(zips)} regression case(s) from {os.path.relpath(CSV_PATH, ROOT)}\n")
    results = []
    for i, name in enumerate(zips, 1):
        print(f"[{i}/{len(zips)}] {name} ...", flush=True)
        results.append(run_case(name, keep))

    print("\n" + "=" * 72)
    print("Regression summary")
    print("=" * 72)
    width = max(len(r['name']) for r in results)
    for r in results:
        print(f"  {r['status']:4}  {r['name']:<{width}}  {r['detail']}")

    failed = [r for r in results if r['status'] != 'PASS']
    print("-" * 72)
    print(f"  {len(results) - len(failed)}/{len(results)} passed")
    if not keep:
        # logs are small; keep them by default for inspection only when failing
        pass
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
