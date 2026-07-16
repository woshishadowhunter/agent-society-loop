"""Run the bundled deterministic scenario without installing the console script."""

from agent_society_loop.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["demo", "--db", "quantum-mug-demo.db"]))
