import subprocess
import sys


def main() -> int:
    print("Applying Alembic migrations...")
    result = subprocess.run(["alembic", "upgrade", "head"])
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
