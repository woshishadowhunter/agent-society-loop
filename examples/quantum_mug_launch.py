"""Run the bundled deterministic scenario without installing the console script."""

from seed_society.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["demo", "--db", "quantum-mug-demo.db"]))
