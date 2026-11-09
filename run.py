"""Run the project without first installing third-party Python packages.

Usage:
    python run.py demo
    python run.py test
    python run.py ingest --batch first
    python run.py report
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        import unittest
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        raise SystemExit(0 if result.wasSuccessful() else 1)
    from parcel_sla.cli import main
    raise SystemExit(main())
