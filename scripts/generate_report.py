"""Rebuild results.csv, diagnostics.csv and report/tables.md from raw files."""
import subprocess, sys

if __name__ == "__main__":
    for args in (["--build"], ["--tables-only"]):
        rc = subprocess.call([sys.executable, "-m", "experiments.analyze", *args])
        if rc:
            sys.exit(rc)
