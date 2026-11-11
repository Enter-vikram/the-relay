"""Run all stand-in load-test scenarios (simulated; never calls Groq).
    python -m scripts.run_load_tests --run-id lt-2
Raw files are immutable, so choose a new --run-id each time."""
import subprocess, sys


def main():
    a = sys.argv[1:] or ["--run-id", "lt-new"]
    sys.exit(subprocess.call([sys.executable, "-m", "experiments.load_test", "--all", "--seed", "1", *a]))


if __name__ == "__main__":
    main()
