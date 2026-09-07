"""Validate locally, check Discord token, then publish all channels."""
from __future__ import annotations

import subprocess
import sys


def run(cmd: list[str]) -> int:
    print(f"\n>> {' '.join(cmd)}\n")
    return subprocess.call(cmd)


def main() -> int:
    py = sys.executable
    force = "force" in sys.argv

    if run([py, "validate_local.py"]) != 0:
        return 1

    if run([py, "check_token.py"]) != 0:
        print(
            "\nDiscord login failed — update DISCORD_TOKEN in .env then rerun:\n"
            f"  {py} run_publish.py force\n"
        )
        return 1

    args = [py, "sync_all.py"]
    if force:
        args.append("force")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
