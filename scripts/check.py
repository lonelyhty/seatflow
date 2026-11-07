"""Run offline tests with disposable application data, never saved user accounts."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


def main() -> int:
    if sys.platform != 'win32':
        print('The full suite requires Windows for DPAPI and Tk desktop checks.')
        return 2
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    previous_directory = Path.cwd()
    previous_data = os.environ.get('SEATFLOW_DATA_DIR')
    try:
        with tempfile.TemporaryDirectory(prefix='seatflow-tests-') as temporary:
            os.environ['SEATFLOW_DATA_DIR'] = temporary
            os.chdir(root)
            suite = unittest.defaultTestLoader.discover(str(root / 'seat_reserver'), top_level_dir=str(root))
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            return 0 if result.wasSuccessful() else 1
    finally:
        os.chdir(previous_directory)
        if previous_data is None:
            os.environ.pop('SEATFLOW_DATA_DIR', None)
        else:
            os.environ['SEATFLOW_DATA_DIR'] = previous_data


if __name__ == '__main__':
    raise SystemExit(main())
