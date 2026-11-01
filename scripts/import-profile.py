"""Preview, then optionally import a user-reviewed profile over authenticated HTTPS."""
import argparse
import getpass
import json
import os
from pathlib import Path
import ssl
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("--server-url", default=os.environ.get("XBLOOM_SERVER_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--ca-file", default=os.environ.get("XBLOOM_CA_FILE"))
    parser.add_argument("--approve", action="store_true", help="Require typed approval and import the previewed profile")
    args = parser.parse_args()
    try:
        origin = urlsplit(args.server_url)
        if origin.scheme not in {"https", "http"} or not origin.hostname or origin.username or origin.password:
            raise ValueError("Use an HTTP(S) server origin without embedded credentials")
        if origin.path not in {"", "/"} or origin.query or origin.fragment:
            raise ValueError("Server URL must not contain a path, query, or fragment")
        if origin.scheme == "http" and origin.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("LAN imports require HTTPS; HTTP is allowed only for loopback")
        if args.profile.stat().st_size > 1_000_000:
            raise ValueError("Profile file is too large")
        profile = json.loads(args.profile.read_text(encoding="utf-8-sig"))
        token = os.environ.get("XBLOOM_API_TOKEN") or getpass.getpass("API token: ")
        if not token:
            raise ValueError("An API token is required")
        context = ssl.create_default_context(cafile=args.ca_file or None)

        def post(path, payload):
            request = Request(args.server_url.rstrip("/") + path,
                              data=json.dumps(payload, allow_nan=False).encode(), method="POST",
                              headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            with urlopen(request, timeout=30, context=context) as response:
                return json.load(response)

        preview = post("/api/import/preview", {"profile": profile})
        print(json.dumps(preview, indent=2, ensure_ascii=False))
        if not args.approve:
            print("Preview only. No records saved. Run with --approve after reviewing this profile.")
            return 0
        if input("Type IMPORT to save this exact profile: ").strip() != "IMPORT":
            print("Cancelled. No records saved.")
            return 0
        result = post("/api/import/approve", {"profile": profile, "approved": True})
        print(f"Imported bean {result['bean']['id']} and recipe {result['recipe']['id']}.")
        print("No card job was created.")
        return 0
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read(5000)).get("detail", exc.reason)
        except (ValueError, AttributeError):
            detail = exc.reason
        print(f"API error {exc.code}: {detail}", file=sys.stderr)
    except (OSError, ValueError, URLError, KeyError, EOFError) as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        print("If approval was already sent, inspect the library before retrying to avoid duplicates.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
