"""Triage runner for Dump directory zip files.

Tests every zip in Dump/ and reports PASS/FAIL with error details.
Results are saved to Cases/triage_logs/ for inspection.

Usage:
    python triage_dump.py
    python triage_dump.py --workers 4   # parallel (default: 4)
    python triage_dump.py --filter "sekiro"  # only zips matching substring
"""

import csv
import json
import os
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.abspath(__file__))
DUMP_DIR = os.path.join(ROOT, 'Dump')
LOG_DIR = os.path.join(ROOT, 'Cases', 'triage_logs')
RESULTS_CSV = os.path.join(ROOT, 'Cases', 'triage_results.csv')

BASE_CONFIG = {
    "log_file_path": "",
    "data_path": "",
    "early_z": True,
    "log_to_file": True,
    "printSyntaxTree": False,
    "print_interpreter_result": False,
    "print_sequence": 100,
    "float_tolerance": 0.005,
    "pixel_tolerance": 0.15,
    "execute_count": -1,
    "max_workers": 1,
    "mesh_view_enabled": False,
    "log_file_mode": "w",
    "float32_emulation": True,
}

_PASSED_RE = re.compile(r"Total PASSED rows:\s*(\d+)\s*/\s*(\d+)")
_print_lock = threading.Lock()


def analyze_log(log_path):
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


def get_first_errors(log_path, n=5):
    """Return first n Error: lines from log."""
    errors = []
    if not os.path.exists(log_path):
        return errors
    with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            if line.lstrip().startswith('Error:'):
                errors.append(line.strip())
                if len(errors) >= n:
                    break
    return errors


def run_case(zip_name, idx, total, vs_only=False, timeout=300):
    stem = os.path.splitext(zip_name)[0]
    os.makedirs(LOG_DIR, exist_ok=True)
    # log_file_path in config is relative to the config file (which lives in Cases/)
    log_rel = os.path.join('triage_logs', stem + '.log')
    log_abs = os.path.join(LOG_DIR, stem + '.log')

    config = dict(BASE_CONFIG)
    config['data_path'] = './Dump/' + zip_name
    config['log_file_path'] = log_rel
    if vs_only:
        config['vs_only'] = True
    cfg_path = os.path.join(ROOT, 'Cases', '.triage_tmp_' + stem + '.json')
    with open(cfg_path, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4)

    result = {'name': zip_name, 'status': 'FAIL', 'detail': '', 'errors': []}
    try:
        proc = subprocess.run(
            [sys.executable, 'render.py', os.path.relpath(cfg_path, ROOT)],
            cwd=ROOT, capture_output=True, text=True, timeout=timeout,
        )
        error_count, passed, total_rows = analyze_log(log_abs)
        if proc.returncode != 0:
            result['detail'] = f"exit={proc.returncode}: {(proc.stdout + proc.stderr).strip()[-300:]}"
        elif error_count is None:
            result['detail'] = "no log produced"
        elif total_rows is None:
            result['detail'] = "no VS-vs-golden comparison in log"
        elif error_count > 0:
            result['detail'] = f"{error_count} Error: lines; passed {passed}/{total_rows}"
            result['errors'] = get_first_errors(log_abs, 3)
        elif passed != total_rows:
            result['detail'] = f"passed {passed}/{total_rows}"
        else:
            result['status'] = 'PASS'
            result['detail'] = f"passed {passed}/{total_rows}"
    except subprocess.TimeoutExpired:
        result['detail'] = f"timeout (>{timeout}s)"
    finally:
        if os.path.exists(cfg_path):
            os.remove(cfg_path)

    with _print_lock:
        status = result['status']
        print(f"  [{idx}/{total}] {status:4} {zip_name}  {result['detail']}", flush=True)

    return result


def main():
    args = sys.argv[1:]
    workers = 4
    name_filter = None
    list_file = None
    vs_only = False
    timeout = 300
    for i, a in enumerate(args):
        if a == '--workers' and i + 1 < len(args):
            workers = int(args[i + 1])
        if a == '--filter' and i + 1 < len(args):
            name_filter = args[i + 1]
        if a == '--list' and i + 1 < len(args):
            list_file = args[i + 1]
        if a == '--timeout' and i + 1 < len(args):
            timeout = int(args[i + 1])
        if a == '--vs-only':
            vs_only = True

    if list_file:
        # Explicit list of zip names (one per line; '#'/blank lines ignored).
        with open(list_file, encoding='utf-8') as f:
            wanted = {ln.strip() for ln in f if ln.strip() and not ln.startswith('#')}
        zips = sorted(z for z in (f for f in os.listdir(DUMP_DIR) if f.endswith('.zip'))
                      if z in wanted)
    else:
        zips = sorted(f for f in os.listdir(DUMP_DIR) if f.endswith('.zip'))
    if name_filter:
        zips = [z for z in zips if name_filter in z]

    mode = "vs_only" if vs_only else "full-pipeline"
    print(f"Triaging {len(zips)} zips from Dump/ with {workers} worker(s), "
          f"{mode}, timeout={timeout}s\n")

    results = []
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(run_case, z, i + 1, len(zips), vs_only, timeout): z
                       for i, z in enumerate(zips)}
            for fut in as_completed(futures):
                results.append(fut.result())
        results.sort(key=lambda r: r['name'])
    else:
        for i, z in enumerate(zips):
            results.append(run_case(z, i + 1, len(zips), vs_only, timeout))

    passed = [r for r in results if r['status'] == 'PASS']
    failed = [r for r in results if r['status'] != 'PASS']

    print("\n" + "=" * 80)
    print(f"TRIAGE SUMMARY: {len(passed)}/{len(results)} passed")
    print("=" * 80)
    if failed:
        print("\nFAILED cases:")
        for r in failed:
            print(f"  FAIL  {r['name']}")
            print(f"        {r['detail']}")
            for e in r['errors']:
                print(f"        {e}")

    # Save results CSV
    with open(RESULTS_CSV, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['filename', 'status', 'detail'])
        for r in results:
            writer.writerow([r['name'], r['status'], r['detail']])
    print(f"\nResults saved to {os.path.relpath(RESULTS_CSV, ROOT)}")
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
