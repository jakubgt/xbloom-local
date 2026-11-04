import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000).hex()


def load_auth(root):
    path = Path(root) / "auth.json"
    return json.loads(path.read_text("utf-8")) if path.exists() else None


def save_auth(root, config):
    path = Path(root) / "auth.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(config), encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    temporary.replace(path)


def auth_mode():
    mode = os.environ.get("XBLOOM_AUTH_MODE", "password").strip().lower()
    if mode not in {"password", "trusted_lan"}:
        raise ValueError("XBLOOM_AUTH_MODE must be password or trusted_lan")
    return mode


def has_password(config):
    return bool(config and all(isinstance(config.get(key), str) and config[key]
                               for key in ("username", "salt", "password_hash")))


def configure_auth(root, username, password):
    if len(password) < 12 or len(password) > 1024:
        raise ValueError("Choose a password with 12–1024 characters")
    if not username.strip() or len(username) > 80:
        raise ValueError("Username is required (maximum 80 characters)")
    salt = secrets.token_hex(16)
    # Enabling password access later must not discard existing bridge/API tokens.
    existing = load_auth(root) or {}
    config = {"username": username.strip(), "salt": salt, "password_hash": password_hash(password, salt),
              "tokens": existing.get("tokens", {})}
    save_auth(root, config)
    return config


def valid_password(config, username, password):
    return bool(has_password(config) and len(password) <= 1024
                and hmac.compare_digest(config["username"].encode(), username.encode())
                and hmac.compare_digest(config["password_hash"], password_hash(password, config["salt"])))
