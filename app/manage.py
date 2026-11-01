"""Local-only administration. Never puts credentials on the command line."""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import zipfile
from contextlib import closing

from .security import configure_auth, load_auth
from .storage import Store


def restore(archive_path, root):
    root = Path(root).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError("Restore requires an empty data directory. Stop the server and choose a new directory; keep the existing data as your rollback.")
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        if sum(e.file_size for e in entries) > 2 * 1024**3 or len(entries) > 100000:
            raise ValueError("Backup is too large")
        if len({e.filename for e in entries}) != len(entries):
            raise ValueError("Duplicate archive paths")
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in entry.filename or ":" in entry.filename:
                raise ValueError("Unsafe backup path")
            if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Links are not allowed in backups")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("schema_version") != 1:
            raise ValueError("Unsupported backup version")
        expected = manifest["files"]
        if set(expected) != {e.filename for e in entries if e.filename != "manifest.json"}:
            raise ValueError("Backup manifest does not match archive")
        if not {"xbloom.sqlite3", "auth.json"}.issubset(expected):
            raise ValueError("Missing database or account settings")
        for name, checksum in expected.items():
            if hashlib.sha256(archive.read(name)).hexdigest() != checksum:
                raise ValueError(f"Backup checksum mismatch: {name}")
        root.mkdir(parents=True, exist_ok=True)
        for name in expected:
            target = root.joinpath(*PurePosixPath(name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))
        with closing(sqlite3.connect(root / "xbloom.sqlite3")) as db, db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Restored database failed integrity check")
            db.execute("DELETE FROM sessions")
            for ident, data in db.execute("SELECT id,data FROM records WHERE kind='jobs'").fetchall():
                job = json.loads(data)
                if job["status"] in {"queued", "running"}:
                    job.update(status="cancelled", error="Cancelled during backup restore; new approval required")
                    db.execute("UPDATE records SET data=? WHERE kind='jobs' AND id=?", (json.dumps(job), ident))
            db.execute("DELETE FROM records WHERE kind='system' AND id='bridge'")
        try:
            os.chmod(root, 0o700)
            os.chmod(root / "auth.json", 0o600)
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(description="Manage your local xBloom library")
    parser.add_argument("command", choices=["setup", "check", "restore"])
    parser.add_argument("archive", nargs="?")
    parser.add_argument("--data-dir", default=os.environ.get("XBLOOM_DATA_DIR", "data"))
    args = parser.parse_args()
    try:
        if args.command == "restore":
            if not args.archive:
                parser.error("restore requires a backup ZIP")
            restore(args.archive, args.data_dir)
            print("Backup restored. Start the server with the restored data directory.")
        else:
            store = Store(args.data_dir)
            if args.command == "check":
                print("Account configured" if load_auth(store.root) else "Account setup required")
                print(f"Database: {store.path}")
            else:
                if load_auth(store.root):
                    raise ValueError("An account already exists. Setup cannot overwrite existing credentials.")
                username = input("Username [admin]: ").strip() or "admin"
                password = getpass.getpass("Password (at least 12 characters): ")
                if password != getpass.getpass("Confirm password: "):
                    raise ValueError("Passwords did not match")
                configure_auth(store.root, username, password)
                print("Account created. Sign in to the dashboard and create API/bridge tokens in Settings.")
    except (ValueError, OSError, zipfile.BadZipFile, KeyError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
