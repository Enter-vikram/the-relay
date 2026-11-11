"""Retrieval-only benchmark (no model calls, no quota) for settings A/B/C.
Requires benchmarks/questions.jsonl written by you from the real PDFs."""
import subprocess, sys

if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "experiments.retrieval_benchmark", *sys.argv[1:]]))
