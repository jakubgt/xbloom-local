"""Regenerate portable, hash-checked runtime and development requirements.

Install requirements-dev.txt first. Normal runs retain existing compatible pins;
use --upgrade-package NAME or --upgrade for a deliberate dependency update.
--check resolves into temporary files and never modifies the checked-in locks.
Resolution may contact the public PyPI index, even when checking existing locks.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


UV_VERSION = "0.13.7"
ROOT = Path(__file__).resolve().parent.parent
LOCKS = ("requirements", "requirements-dev")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if regeneration changes a lock")
    updates = parser.add_mutually_exclusive_group()
    updates.add_argument("--upgrade", action="store_true", help="Allow all compatible transitive updates")
    updates.add_argument("--upgrade-package", action="append", default=[], metavar="NAME")
    args = parser.parse_args()
    try:
        installed = importlib.metadata.version("uv")
    except importlib.metadata.PackageNotFoundError:
        parser.error("Install the checked-in requirements-dev.txt hash lock first (uv is missing).")
    if installed != UV_VERSION:
        parser.error(f"Expected uv {UV_VERSION}, found {installed}; install requirements-dev.txt.")

    # Do not inherit alternative indexes, credentials, or local resolver settings.
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(("UV_", "PIP_"))}
    environment["UV_NO_PROGRESS"] = "1"
    cache = ROOT / ".local" / "dependency-cache"
    with tempfile.TemporaryDirectory(prefix="xbloom-lock-") as temporary:
        directory = Path(temporary)
        for name in LOCKS:
            shutil.copyfile(ROOT / f"{name}.in", directory / f"{name}.in")
            existing = ROOT / f"{name}.txt"
            if existing.is_file():
                shutil.copyfile(existing, directory / existing.name)
        for name in LOCKS:
            command = [sys.executable, "-m", "uv", "pip", "compile", f"{name}.in",
                       "--output-file", f"{name}.txt", "--universal", "--python-version", "3.11",
                       "--generate-hashes", "--only-binary", ":all:", "--fork-strategy", "fewest",
                       "--no-config", "--default-index", "https://pypi.org/simple",
                       "--keyring-provider", "disabled", "--no-python-downloads",
                       "--python", sys.executable, "--cache-dir", str(cache),
                       "--custom-compile-command", "python scripts/lock-dependencies.py", "--quiet"]
            if args.upgrade:
                command.append("--upgrade")
            for package in args.upgrade_package:
                command.extend(("--upgrade-package", package))
            subprocess.run(command, cwd=directory, env=environment, check=True)
        changed = []
        for name in LOCKS:
            generated = (directory / f"{name}.txt").read_text(encoding="utf-8")
            target = ROOT / f"{name}.txt"
            if not target.is_file() or target.read_text(encoding="utf-8") != generated:
                changed.append(target.name)
                if not args.check:
                    target.write_text(generated, encoding="utf-8", newline="\n")
        if args.check and changed:
            print("Dependency locks are out of date: " + ", ".join(changed), file=sys.stderr)
            return 1
        print("Dependency locks are current." if args.check else
              "Updated " + (", ".join(changed) if changed else "no dependency locks") + ".")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
