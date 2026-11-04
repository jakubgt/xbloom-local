"""LAN-only FastAPI application. Hardware execution lives in bridge/, never here."""
import hashlib
import hmac
import io
import ipaddress
import json
import os
import secrets
import time
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Body, Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .initialization import (CAPABILITY as INITIALIZATION_CAPABILITY, CHIP_PROFILE,
    InitializationError, bind_initialization_plan, build_initialization_plan,
    initialization_snapshot, validate_initialization_job, validate_initialization_readback)
from .models import Bean, Card, Profile, Recipe
from .nfc import decode_recipe, parse_dump, preview_recipe
from .preferences import BrewingDefaults, apply_brewing_defaults, brewing_defaults_view
from .product_import import ProductLookupError, lookup_product
from .security import auth_mode, configure_auth, digest, has_password, load_auth, save_auth, valid_password
from .storage import Store, new_id, now


def create_app(data_dir=None):
    access_mode = auth_mode()
    lan_only = os.environ.get("XBLOOM_LAN_ONLY", "true").lower() != "false"
    allowed_hosts = [host.strip() for host in os.environ.get(
        "XBLOOM_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],testserver").split(",") if host.strip()]
    if access_mode == "trusted_lan" and (not lan_only or not allowed_hosts or any("*" in host for host in allowed_hosts)):
        raise ValueError("Trusted LAN access requires XBLOOM_LAN_ONLY=true and explicit XBLOOM_ALLOWED_HOSTS")
    store = Store(data_dir or os.environ.get("XBLOOM_DATA_DIR", "data"))
    if access_mode == "trusted_lan":
        with store.lock:
            if load_auth(store.root) is None:
                save_auth(store.root, {"tokens": {}})
    app = FastAPI(title="xBloom Local", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    failures = {}
    secure_cookies = os.environ.get("XBLOOM_SECURE_COOKIES", "false").lower() == "true"
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    def same_origin(request, origin):
        try:
            origin_url = urlsplit(origin)
            return origin_url.netloc == request.headers.get("host") and origin_url.scheme == request.url.scheme
        except ValueError:
            return False

    @app.middleware("http")
    async def boundaries(request, call_next):
        host = request.client.host if request.client else ""
        if lan_only and host != "testclient":
            try:
                addr = ipaddress.ip_address(host)
                if not (addr.is_private or addr.is_loopback):
                    return JSONResponse({"detail": "Local network access only"}, status_code=403)
            except ValueError:
                return JSONResponse({"detail": "Unrecognized client address"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and not same_origin(request, origin):
                return JSONResponse({"detail": "Cross-origin requests are not allowed"}, status_code=403)
            try:
                if int(request.headers.get("content-length", 0)) > 12 * 1024 * 1024:
                    return JSONResponse({"detail": "Request is too large"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "Invalid content length"}, status_code=400)
        result = await call_next(request)
        result.headers["X-Content-Type-Options"] = "nosniff"
        result.headers["Referrer-Policy"] = "no-referrer"
        result.headers["X-Frame-Options"] = "DENY"
        result.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.url.path.startswith("/api"):
            result.headers["Cache-Control"] = "no-store"
        return result

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        messages = [f"{'.'.join(str(x) for x in e['loc'][1:])}: {e['msg']}" for e in exc.errors()]
        return JSONResponse({"detail": "; ".join(messages)}, status_code=422)

    def record(kind, ident):
        value = store.get(kind, ident)
        if value is None:
            raise HTTPException(404, "Record not found")
        return value

    def session_digest(token):
        # A login-free session must never authorize password-protected access.
        return digest(f"trusted_lan:{token}" if access_mode == "trusted_lan" else token)

    def cookie_valid(request):
        token = request.cookies.get("xbloom_session")
        if not token:
            return False
        with store.connect() as db:
            row = db.execute("SELECT expires FROM sessions WHERE token_hash=?", (session_digest(token),)).fetchone()
        return bool(row and row[0] > time.time())

    def token_valid(request, kind):
        auth = load_auth(store.root)
        header = request.headers.get("authorization", "")
        expected = (auth or {}).get("tokens", {}).get(kind)
        return bool(expected and header.startswith("Bearer ") and hmac.compare_digest(expected, digest(header[7:])))

    def user(request: Request):
        if not cookie_valid(request) and not token_valid(request, "api"):
            raise HTTPException(401, "Refresh the dashboard to continue" if access_mode == "trusted_lan" else "Sign in to continue")

    def session_user(request: Request):
        if not cookie_valid(request):
            raise HTTPException(401, "Refresh the dashboard before this action" if access_mode == "trusted_lan" else "This action requires dashboard sign-in")

    def bridge_user(request: Request):
        if not token_valid(request, "bridge"):
            raise HTTPException(401, "Bridge authentication required")

    def login_cookie(response):
        value = secrets.token_urlsafe(32)
        with store.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
            db.execute("INSERT INTO sessions VALUES (?,?)", (session_digest(value), time.time() + 12 * 3600))
        response.set_cookie("xbloom_session", value, httponly=True, secure=secure_cookies, samesite="strict", max_age=43200)

    @app.get("/api/session")
    def get_session(request: Request, response: Response):
        auth = load_auth(store.root)
        authenticated = cookie_valid(request)
        if access_mode == "trusted_lan":
            # Bootstrap only from this dashboard, never from an embedded/foreign
            # site. Non-browser LAN clients may omit Fetch Metadata and Origin.
            origin = request.headers.get("origin")
            if origin and not same_origin(request, origin):
                raise HTTPException(403, "Cross-origin requests are not allowed")
            if request.headers.get("sec-fetch-site", "none") not in {"same-origin", "none"}:
                raise HTTPException(403, "Open the dashboard directly on your local network")
            # Scoped API/bridge credentials do not become dashboard sessions.
            if not authenticated and not request.headers.get("authorization"):
                login_cookie(response)
                authenticated = True
        return {"configured": access_mode == "trusted_lan" or has_password(auth), "authenticated": authenticated,
                "username": auth.get("username") if access_mode == "password" and authenticated and auth else None,
                "access_mode": access_mode}

    @app.post("/api/setup")
    def setup(request: Request, response: Response, payload: dict = Body(...)):
        if access_mode == "trusted_lan":
            raise HTTPException(409, "Dashboard sign-in is disabled for trusted LAN access")
        client = request.client.host if request.client else ""
        if client not in {"127.0.0.1", "::1", "testclient"}:
            raise HTTPException(403, "Initial setup must run on localhost or with python -m app.manage setup")
        with store.lock:
            if has_password(load_auth(store.root)):
                raise HTTPException(409, "Already configured")
            try:
                config = configure_auth(store.root, str(payload.get("username", "admin")), str(payload.get("password", "")))
            except ValueError as exc:
                raise HTTPException(422, str(exc))
            login_cookie(response)
        store.audit("Local account created")
        return {"configured": True, "authenticated": True, "username": config["username"], "access_mode": access_mode}

    @app.post("/api/session")
    def login(request: Request, response: Response, payload: dict = Body(...)):
        if access_mode == "trusted_lan":
            raise HTTPException(409, "Dashboard sign-in is disabled for trusted LAN access")
        key = request.client.host if request.client else "unknown"
        recent = [t for t in failures.get(key, []) if t > time.time() - 300]
        failures[key] = recent
        if len(recent) >= 10:
            raise HTTPException(429, "Too many sign-in attempts; try again in five minutes")
        auth = load_auth(store.root)
        if not valid_password(auth, str(payload.get("username", "")), str(payload.get("password", ""))):
            recent.append(time.time())
            raise HTTPException(401, "Incorrect username or password")
        failures.pop(key, None)
        login_cookie(response)
        return {"configured": True, "authenticated": True, "username": auth["username"], "access_mode": access_mode}

    @app.delete("/api/session")
    def logout(request: Request, response: Response):
        with store.connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (session_digest(request.cookies.get("xbloom_session", "")),))
        response.delete_cookie("xbloom_session")
        return {"ok": True}

    def bridge_status():
        status = store.get("system", "bridge") or {"online": False, "mode": None, "name": "Windows bridge", "last_seen": None}
        status["online"] = bool(status.get("last_seen_epoch", 0) > time.time() - 45)
        return status

    @app.get("/api/status", dependencies=[Depends(user)])
    def status():
        return {"beans_count": len(store.all("beans")), "recipes_count": len(store.all("recipes")),
                "cards_count": len(store.all("cards")), "bridge": bridge_status(),
                "recent_activity": store.all("activity")[:20], "setup_complete": True, "version": __version__}

    def saved_brewing_defaults():
        saved = store.get("system", "brewing-defaults") or {}
        return {"equipment_profile": saved.get("equipment_profile", "omni")}

    @app.get("/api/settings/brewing-defaults", dependencies=[Depends(user)])
    def get_brewing_defaults():
        return brewing_defaults_view(saved_brewing_defaults())

    @app.patch("/api/settings/brewing-defaults", dependencies=[Depends(session_user)])
    def update_brewing_defaults(payload: BrewingDefaults):
        if "equipment_profile" not in payload.model_fields_set:
            raise HTTPException(422, "Choose an equipment profile")
        with store.lock:
            store.put("system", {"id": "brewing-defaults", **payload.model_dump(), "updated_at": now()})
        store.audit("Updated the equipment defaults for future recipes")
        return brewing_defaults_view(payload.model_dump())

    @app.get("/api/settings", dependencies=[Depends(user)])
    def settings():
        config = load_auth(store.root)
        return {"username": config.get("username") if access_mode == "password" else None,
                "access_mode": access_mode, "version": __version__, "bridge": bridge_status(),
                "tokens_configured": list(config.get("tokens", {})), "default_dose_g": 18,
                "storage": "Local SQLite and files", "machine_connectivity": "Offline / NFC only",
                "brewing_defaults": brewing_defaults_view(saved_brewing_defaults())}

    @app.post("/api/tokens", dependencies=[Depends(session_user)])
    def create_token(payload: dict = Body(...)):
        kind = payload.get("kind")
        if kind not in {"api", "bridge"}:
            raise HTTPException(422, "Token kind must be api or bridge")
        value = secrets.token_urlsafe(40)
        with store.lock:
            config = load_auth(store.root)
            config["tokens"][kind] = digest(value)
            save_auth(store.root, config)
        store.audit(f"{kind.title()} token rotated")
        return {"token": value, "kind": kind, "notice": "Shown once. Store securely; the previous token has been revoked."}

    @app.get("/api/beans", dependencies=[Depends(user)])
    def beans():
        return {"items": store.all("beans")}

    @app.post("/api/beans", dependencies=[Depends(user)])
    def create_bean(payload: Bean):
        item = store.create("beans", payload.model_dump())
        store.audit(f"Added beans: {item['name']}")
        return item

    @app.get("/api/beans/{ident}", dependencies=[Depends(user)])
    def get_bean(ident: str):
        return record("beans", ident)

    @app.patch("/api/beans/{ident}", dependencies=[Depends(user)])
    def update_bean(ident: str, payload: dict = Body(...)):
        with store.lock:
            old = record("beans", ident)
            try:
                checked = Bean.model_validate({**{k: v for k, v in old.items() if k in Bean.model_fields}, **payload}).model_dump()
            except ValueError as exc:
                raise HTTPException(422, str(exc))
            result = store.put("beans", {**old, **checked, "updated_at": now()})
        return result

    @app.delete("/api/beans/{ident}", dependencies=[Depends(user)])
    def delete_bean(ident: str):
        record("beans", ident)
        if any(r.get("bean_id") == ident for r in store.all("recipes")):
            raise HTTPException(409, "Remove or reassign this bean's recipes first")
        store.delete("beans", ident)
        return {"ok": True}

    @app.post("/api/beans/{ident}/photo", dependencies=[Depends(user)])
    async def upload_photo(ident: str, file: UploadFile = File(...)):
        bean = record("beans", ident)
        data = await file.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise HTTPException(413, "Photos must be under 8 MB")
        extension = ".png" if data.startswith(b"\x89PNG\r\n\x1a\n") else ".jpg" if data.startswith(b"\xff\xd8\xff") else ".webp" if data[:4] == b"RIFF" and data[8:12] == b"WEBP" else None
        if not extension:
            raise HTTPException(422, "Use a PNG, JPEG or WebP image")
        directory = store.root / "photos"
        directory.mkdir(exist_ok=True)
        filename = new_id() + extension
        (directory / filename).write_bytes(data)
        bean["photo_url"] = f"/api/photos/{filename}"
        store.put("beans", bean)
        return bean

    @app.get("/api/photos/{filename}", dependencies=[Depends(user)])
    def photo(filename: str):
        if Path(filename).name != filename or not filename.endswith((".jpg", ".png", ".webp")):
            raise HTTPException(404)
        path = store.root / "photos" / filename
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path)

    def validate_recipe_bean(recipe):
        if recipe.get("bean_id"):
            record("beans", recipe["bean_id"])

    def persist_recipe(payload, new_bean=None):
        if new_bean is None:
            validate_recipe_bean(payload)
        stamp = now()
        result = {**payload, "version": 1, "id": new_id(), "created_at": stamp, "updated_at": stamp}
        version = {"id": new_id(), "recipe_id": result["id"], "version": 1, "recipe": result, "created_at": stamp, "updated_at": stamp}
        store.put_all(([('beans', new_bean)] if new_bean else []) + [("recipes", result), ("versions", version)])
        store.audit(f"Saved recipe: {result['name']} · v1")
        return result

    @app.get("/api/recipes", dependencies=[Depends(user)])
    def recipes():
        return {"items": store.all("recipes")}

    @app.post("/api/recipes", dependencies=[Depends(user)])
    def create_recipe(payload: Recipe):
        return persist_recipe(payload.model_dump())

    @app.post("/api/recipes/preview", dependencies=[Depends(user)])
    def recipe_preview(payload: Recipe):
        return preview_recipe(payload.model_dump())

    @app.get("/api/recipes/{ident}", dependencies=[Depends(user)])
    def get_recipe(ident: str):
        return record("recipes", ident)

    @app.patch("/api/recipes/{ident}", dependencies=[Depends(user)])
    def update_recipe(ident: str, payload: dict = Body(...)):
        with store.lock:
            old = record("recipes", ident)
            try:
                checked = Recipe.model_validate({**{k: v for k, v in old.items() if k in Recipe.model_fields}, **payload}).model_dump()
            except ValueError as exc:
                raise HTTPException(422, str(exc))
            validate_recipe_bean(checked)
            stamp = now()
            result = {**old, **checked, "version": old["version"] + 1, "updated_at": stamp}
            version = {"id": new_id(), "recipe_id": ident, "version": result["version"], "recipe": result, "created_at": stamp, "updated_at": stamp}
            store.put_all([("recipes", result), ("versions", version)])
            store.audit(f"Updated recipe: {result['name']} · v{result['version']}")
        return result

    @app.get("/api/recipes/{ident}/versions", dependencies=[Depends(user)])
    def recipe_versions(ident: str):
        record("recipes", ident)
        return {"items": [v for v in store.all("versions") if v["recipe_id"] == ident]}

    @app.delete("/api/recipes/{ident}", dependencies=[Depends(user)])
    def delete_recipe(ident: str):
        record("recipes", ident)
        if any(c.get("recipe_id") == ident for c in store.all("cards")) or any(j.get("recipe_id") == ident and j["status"] in {"queued", "running"} for j in store.all("jobs")):
            raise HTTPException(409, "This recipe is assigned to a card or an active job")
        store.delete("recipes", ident)
        return {"ok": True}

    def profile_payload(payload):
        try:
            profile = Profile.model_validate(payload.get("profile", payload)).model_dump()
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        warnings = []
        if not profile["bean"].get("product_url"):
            warnings.append("No product URL supplied. Source details remain unverified.")
        if not profile["bean"].get("provenance"):
            warnings.append("No field-level source notes supplied. Review bean facts before approval.")
        warnings.append("Brewing settings are recommendations; validate them by tasting.")
        return profile, warnings

    @app.post("/api/import/from-url", dependencies=[Depends(user)])
    def import_from_url(payload: dict = Body(...)):
        try:
            result = lookup_product(payload.get("url"))
            defaults = saved_brewing_defaults()
            result["profile"]["recipe"] = apply_brewing_defaults(result["profile"]["recipe"], defaults)
            if defaults["equipment_profile"] == "v60_01_melodrip":
                result.setdefault("warnings", []).append("Using your V60 01 + Melodrip defaults: automatic grinding with your moving V60 adapter, then manual Melodrip placement after the bloom.")
            return result
        except ProductLookupError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc

    @app.post("/api/import/preview", dependencies=[Depends(user)])
    def import_preview(payload: dict = Body(...)):
        profile, warnings = profile_payload(payload)
        return {**profile, "warnings": warnings, "encoding": preview_recipe(profile["recipe"])}

    @app.post("/api/import/approve", dependencies=[Depends(user)])
    def import_approve(payload: dict = Body(...)):
        if payload.get("approved") is not True:
            raise HTTPException(422, "Review the profile and explicitly approve import")
        profile, _ = profile_payload(payload)
        with store.lock:
            bean = {**profile["bean"], "id": new_id(), "created_at": now(), "updated_at": now()}
            recipe = persist_recipe({**profile["recipe"], "bean_id": bean["id"]}, new_bean=bean)
        store.audit(f"Imported approved profile: {bean['name']}")
        return {"bean": bean, "recipe": recipe}

    def card_view(card):
        backups = [b for b in store.all("backups") if b["card_id"] == card["id"]]
        counts = {f"{source if source != 'import' else 'imported'}_backup_count": sum(b["source"] == source for b in backups) for source in ("hardware", "simulation", "import")}
        return {**card, **counts, "backup_count": len(backups), "last_backup_at": backups[0]["created_at"] if backups else None}

    @app.get("/api/cards", dependencies=[Depends(user)])
    def cards():
        return {"items": [card_view(c) for c in store.all("cards")]}

    @app.post("/api/cards", dependencies=[Depends(user)])
    def create_card(payload: Card):
        if any(c["label"].casefold() == payload.label.casefold() for c in store.all("cards")):
            raise HTTPException(409, "Choose a unique physical label for this card")
        card = store.create("cards", {**payload.model_dump(), "recipe_id": None, "verified_at": None})
        store.audit(f"Registered card: {card['label']}")
        return card_view(card)

    @app.patch("/api/cards/{ident}", dependencies=[Depends(user)])
    def update_card(ident: str, payload: dict = Body(...)):
        with store.lock:
            card = record("cards", ident)
            if any(j["card_id"] == ident and j["status"] in {"queued", "running"} for j in store.all("jobs")):
                raise HTTPException(409, "Finish or cancel the active card job first")
            try:
                checked = Card.model_validate({**{k: v for k, v in card.items() if k in Card.model_fields}, **payload}).model_dump()
            except ValueError as exc:
                raise HTTPException(422, str(exc))
            if any(c["id"] != ident and c["label"].casefold() == checked["label"].casefold() for c in store.all("cards")):
                raise HTTPException(409, "Card labels must be unique")
            if checked["uid"] != card.get("uid") and any(b["card_id"] == ident for b in store.all("backups")):
                raise HTTPException(409, "A backed-up card's UID cannot be changed; register another card")
            return card_view(store.put("cards", {**card, **checked, "updated_at": now()}))

    @app.delete("/api/cards/{ident}", dependencies=[Depends(user)])
    def delete_card(ident: str):
        card = record("cards", ident)
        if any(b["card_id"] == ident for b in store.all("backups")):
            raise HTTPException(409, "Cards with preserved backups cannot be deleted")
        if any(j["card_id"] == ident and j["status"] in {"queued", "running"} for j in store.all("jobs")):
            raise HTTPException(409, "Cancel the active card job first")
        store.delete("cards", card["id"])
        return {"ok": True}

    def parse_or_error(dump):
        try:
            return parse_dump(dump)
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(422, f"Invalid card dump: {exc}")

    def save_backup(card, dump, source, job_id=None):
        parsed = parse_or_error(dump)
        if card.get("uid") and card["uid"] != parsed["uid"]:
            raise HTTPException(409, "Dump UID does not match this card")
        previous = [b for b in store.all("backups") if b["card_id"] == card["id"] and b["source"] == source]
        bid = new_id()
        directory = store.root / "card-backups" / card["id"]
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{bid}.json"
        with path.open("x", encoding="utf-8") as output:
            json.dump(dump, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
        backup = store.create("backups", {"card_id": card["id"], "file": str(path.relative_to(store.root)),
            "sha256": parsed["sha256"], "uid": parsed["uid"], "block_count": parsed["block_count"],
            "memory_hex": parsed["memory_hex"], "source": source, "original": not previous and not source.startswith("failed-readback"),
            "job_id": job_id, "decoded": parsed.get("decoded"), "locks": parsed.get("locks", [])})
        card["uid"] = parsed["uid"]
        store.put("cards", card)
        return backup

    def native_backup(backup):
        """Read immutable source evidence and verify it against its saved record."""
        try:
            path = (store.root / backup["file"]).resolve()
            path.relative_to(store.root)
            dump = json.loads(path.read_text(encoding="utf-8"))
            snapshot = initialization_snapshot(dump)
            for key in ("uid", "sha256", "memory_hex", "block_count", "locks"):
                if snapshot[key] != backup.get(key):
                    raise ValueError("Saved backup record does not match its native file")
            return dump, snapshot
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise HTTPException(422, f"Initialization needs intact native backup evidence: {exc}")

    def manual_initialization_review(card, mode):
        modes = card.get("manual_identity_reconciliation_modes")
        return mode in modes if isinstance(modes, list) else bool(card.get("requires_manual_identity_reconciliation"))

    def mark_initialization_review(card, mode, job_id):
        card["requires_manual_identity_reconciliation"] = True
        card["manual_identity_reconciliation_modes"] = sorted(set(card.get("manual_identity_reconciliation_modes", []) + [mode]))
        card["initialization_review_job_id"] = job_id

    def initialization_failure_evidence(card, dump, job, *, stage="failed_readback"):
        """Retain unexpected identity only as evidence, never as a live baseline."""
        directory = store.root / "card-backups" / card["id"]
        directory.mkdir(parents=True, exist_ok=True)
        evidence_id = new_id()
        intermediate = stage == "initialization_identity_readback"
        suffix = "initialization-identity-evidence" if intermediate else "initialization-evidence"
        path = directory / f"{evidence_id}-{suffix}.json"
        with path.open("x", encoding="utf-8") as output:
            json.dump(dump, output, indent=2, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        job[f"{stage}_evidence_file"] = str(path.relative_to(store.root))
        try:
            parsed = parse_dump(dump)
        except (ValueError, TypeError, KeyError) as exc:
            job["initialization_identity_evidence_error" if intermediate else "evidence_error"] = f"Raw initialization evidence retained; parsing failed: {exc}"[:1000]
            return
        evidence = store.create("backups", {"card_id": card["id"], "file": str(path.relative_to(store.root)),
            "sha256": parsed["sha256"], "uid": parsed["uid"], "block_count": parsed["block_count"],
            "memory_hex": parsed["memory_hex"], "source": f"{'initialization-identity' if intermediate else 'failed-readback'}-{job['mode']}", "original": False,
            "job_id": job["id"], "decoded": parsed.get("decoded"), "locks": parsed["locks"],
            "initialization_evidence_only": True})
        job[f"{stage}_backup_id"] = evidence["id"]
        job["initialization_identity_observed_uid" if intermediate else "observed_uid"] = parsed["uid"]

    @app.get("/api/cards/{ident}/backups", dependencies=[Depends(user)])
    def backups(ident: str):
        card = record("cards", ident)
        items = []
        for backup in store.all("backups"):
            if backup["card_id"] != ident:
                continue
            item = {**backup, "initialization_native_json": False,
                    "initialization_blank_candidate": False, "initialization_template_candidate": False}
            try:
                _, snapshot = native_backup(backup)
                item.update(native_metadata=snapshot["metadata"], block_size=snapshot["block_size"], initialization_native_json=True)
                supported = (card.get("type") == "custom" and backup["source"] in {"hardware", "simulation"}
                    and snapshot["block_count"] == 64 and snapshot["block_size"] == 4
                    and snapshot["metadata"]["ic"] == "8B" and snapshot["metadata"]["afi"] == "00"
                    and snapshot["metadata"]["dsfid"] == "00")
                memory = bytes.fromhex(snapshot["memory_hex"])
                item["initialization_blank_candidate"] = supported and not any(memory)
                item["initialization_template_candidate"] = bool(supported and any(memory[:32])
                    and (backup.get("decoded") or {}).get("checksum_valid"))
            except HTTPException:
                pass
            items.append(item)
        return {"items": items}

    @app.post("/api/cards/{ident}/backups", dependencies=[Depends(user)])
    def upload_backup(ident: str, payload: dict = Body(...)):
        with store.lock:
            card = record("cards", ident)
            result = save_backup(card, payload.get("dump"), "import")
        store.audit(f"Imported reference backup: {card['label']}")
        return result

    @app.get("/api/cards/{ident}/backups/{backup_id}/download", dependencies=[Depends(user)])
    def download_backup(ident: str, backup_id: str):
        backup = record("backups", backup_id)
        if backup["card_id"] != ident:
            raise HTTPException(404)
        return FileResponse(store.root / backup["file"], filename=f"card-{ident[:8]}-{backup_id[:8]}.json", media_type="application/json")

    def recipe_version(recipe_id, version=None):
        latest = record("recipes", recipe_id)
        if version is None or latest["version"] == version:
            return latest
        for item in store.all("versions"):
            if item["recipe_id"] == recipe_id and item["version"] == version:
                return item["recipe"]
        raise HTTPException(404, "Recipe version not found")

    def latest_backup(card_id, source=None):
        for item in store.all("backups"):
            if item["card_id"] == card_id and (source is None or item["source"] == source):
                return item
        return None

    def initialization_preview_for(card, payload):
        mode = payload.get("bridge_mode")
        if mode not in {"hardware", "simulation"} or bridge_status().get("mode") != mode:
            raise HTTPException(409, "Review initialization in the currently selected bridge mode")
        if card.get("type") != "custom":
            raise HTTPException(422, "Initialization is restricted to custom inventory cards")
        if mode == "simulation" and any(backup["card_id"] == card["id"] and backup["source"] == "hardware" for backup in store.all("backups")):
            raise HTTPException(409, "Use separate simulation inventory; initialization cannot change a physically backed-up card's identity in simulation")
        if mode == "hardware" and card.get("initialization_mode") == "simulation":
            raise HTTPException(409, "Use separate physical inventory; simulated initialization cannot establish a hardware identity")
        if manual_initialization_review(card, mode):
            raise HTTPException(409, "This card requires manual identity reconciliation after initialization; no retry is permitted")
        if mode in card.get("needs_fresh_read_modes", []) or (card.get("needs_fresh_read") and "needs_fresh_read_modes" not in card):
            raise HTTPException(409, "Read this card again before reviewing initialization")
        if payload.get("chip_profile") != CHIP_PROFILE:
            raise HTTPException(422, "Select the explicit factory Gen2 initialization profile")
        if type(payload.get("version")) is not int or payload["version"] < 1:
            raise HTTPException(422, "Initialization requires an exact saved recipe version")
        if any(key in payload for key in ("target_uid", "memory_hex", "template_dump", "write_blocks", "initialization_recipe")):
            raise HTTPException(422, "Initialization accepts saved template and recipe references, never an arbitrary target")
        baseline = latest_backup(card["id"], mode)
        if baseline is None:
            raise HTTPException(409, "Read and preserve this physical card's original backup first")
        current_dump, current = native_backup(baseline)
        if card.get("uid") != current["uid"]:
            raise HTTPException(409, "The current backup identity does not match this inventory card")
        originals = [backup for backup in store.all("backups") if backup["card_id"] == card["id"]
                     and backup["source"] == mode and backup.get("original") is True]
        if len(originals) != 1:
            raise HTTPException(409, "An immutable original backup from this bridge mode must be retained")
        _, original_snapshot = native_backup(originals[0])
        if original_snapshot != current:
            raise HTTPException(409, "The blank card no longer matches its saved original identity, memory, and metadata")
        template = record("backups", payload.get("template_backup_id"))
        template_card = record("cards", template["card_id"])
        if template["source"] != mode or template_card["id"] == card["id"] or template_card.get("type") != "custom":
            raise HTTPException(422, "Choose a saved same-mode template from a different custom inventory card")
        if manual_initialization_review(template_card, mode):
            raise HTTPException(409, "The template card requires manual identity reconciliation")
        template_dump, _ = native_backup(template)
        recipe = recipe_version(payload.get("recipe_id"), payload["version"])
        context = {"card_id": card["id"], "card_label": card["label"], "baseline_backup_id": baseline["id"],
            "original_backup_id": originals[0]["id"], "template_card_id": template_card["id"],
            "template_label": template_card["label"], "template_backup_id": template["id"],
            "recipe_id": recipe["id"], "recipe_version": recipe["version"], "mode": mode}
        try:
            return bind_initialization_plan(build_initialization_plan(current_dump, template_dump, recipe,
                chip_profile=payload["chip_profile"]), context)
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(422, str(exc))

    @app.post("/api/cards/{ident}/initialize-preview", dependencies=[Depends(session_user)])
    def initialization_preview(ident: str, payload: dict = Body(...)):
        with store.lock:
            return initialization_preview_for(record("cards", ident), payload)

    @app.post("/api/cards/{ident}/preview", dependencies=[Depends(user)])
    def card_preview(ident: str, payload: dict = Body(...)):
        record("cards", ident)
        recipe = recipe_version(payload.get("recipe_id"), payload.get("version"))
        backup = latest_backup(ident)
        if not backup:
            raise HTTPException(409, "Import or read a card backup before previewing its payload")
        return {**preview_recipe(recipe, bytes.fromhex(backup["memory_hex"])), "backup_id": backup["id"], "recipe_version": recipe["version"]}

    @app.get("/api/jobs", dependencies=[Depends(user)])
    def jobs():
        return {"items": store.all("jobs")[:100]}

    @app.post("/api/jobs", dependencies=[Depends(session_user)])
    def create_job(payload: dict = Body(...)):
        with store.lock:
            kind = payload.get("kind")
            if kind not in {"read", "write", "restore", "initialize"}:
                raise HTTPException(422, "Choose read, write, restore or explicit initialization")
            card = record("cards", payload.get("card_id"))
            bridge = bridge_status()
            if not bridge["online"]:
                raise HTTPException(409, "Start your Windows bridge before creating a card job")
            if any(j["status"] in {"queued", "running"} for j in store.all("jobs")):
                raise HTTPException(409, "Finish or cancel the current card job first")
            mode = bridge["mode"]
            if payload.get("bridge_mode") != mode:
                raise HTTPException(409, "Bridge mode changed or was not reviewed. Review and confirm the operation again.")
            job = {"kind": kind, "card_id": card["id"], "label": card["label"], "uid": card.get("uid"),
                   "status": "queued", "mode": mode, "confirmed_at": None, "expires_at": time.time() + 600}
            if kind != "read":
                if payload.get("confirm") is not True or payload.get("confirm_label") != card["label"]:
                    raise HTTPException(422, "Explicitly confirm the write and type this physical card's label")
                if manual_initialization_review(card, mode):
                    raise HTTPException(409, "This card requires manual identity reconciliation; no further mutation is permitted")
                if mode in card.get("needs_fresh_read_modes", []) or (card.get("needs_fresh_read") and "needs_fresh_read_modes" not in card):
                    raise HTTPException(409, "Read this card again after the interrupted or failed operation")
                baseline = latest_backup(card["id"], mode)
                if not baseline:
                    raise HTTPException(409, f"Read and back up this card using the {mode} bridge first")
                memory = bytes.fromhex(baseline["memory_hex"])
                if kind == "initialize":
                    if INITIALIZATION_CAPABILITY not in bridge.get("capabilities", []):
                        raise HTTPException(409, "Start the explicitly enabled factory initialization bridge before confirming this operation")
                    if payload.get("confirm_initialize") is not True or payload.get("accept_special_configuration_changes") is not True:
                        raise HTTPException(422, "Explicitly approve initialization and both special configuration writes")
                    plan = initialization_preview_for(card, payload)
                    context = plan["initialization_context"]
                    if payload.get("confirm_template_label") != context["template_label"]:
                        raise HTTPException(422, "Confirm the exact template inventory label")
                    if payload.get("expected_preview_sha256") != plan["preview_sha256"]:
                        raise HTTPException(409, "Initialization preview changed; review the exact original, template, and recipe again")
                    if plan["differences"] and payload.get("accept_encoding_changes") is not True:
                        raise HTTPException(422, "Review and explicitly accept NFC encoding differences before initialization")
                    job.update(plan)
                    job.update({key: context[key] for key in ("original_backup_id", "template_card_id", "template_label",
                        "template_backup_id", "recipe_id", "recipe_version")})
                    job.update(confirm_initialize=True, accept_special_configuration_changes=True,
                        accept_encoding_changes=payload.get("accept_encoding_changes") is True,
                        expected_preview_sha256=plan["preview_sha256"])
                elif kind == "write":
                    recipe = recipe_version(payload.get("recipe_id"), payload.get("version"))
                    preview = preview_recipe(recipe, memory)
                    if not preview["compatible"]:
                        raise HTTPException(422, "; ".join(preview["errors"]))
                    if preview.get("differences") and payload.get("accept_encoding_changes") is not True:
                        raise HTTPException(422, "Review and explicitly accept NFC encoding differences before writing")
                    job.update(recipe_id=recipe["id"], recipe_version=recipe["version"], effective=preview["effective"],
                               memory_hex=preview["memory_hex"], write_blocks=preview["write_blocks"])
                else:
                    original = record("backups", payload.get("backup_id"))
                    if original["card_id"] != card["id"] or original["source"] != mode:
                        raise HTTPException(422, "Restore requires a backup of this physical card from the same bridge mode")
                    target = bytes.fromhex(original["memory_hex"])
                    if len(target) != len(memory) or target[:32] != memory[:32] or target[160:] != memory[160:]:
                        raise HTTPException(422, "This backup changes protected identity/configuration data; recipe-only restore refused")
                    try:
                        restored_recipe = decode_recipe(target)
                        if not restored_recipe["checksum_valid"]:
                            raise ValueError("The selected backup's recipe checksum is invalid")
                    except ValueError as exc:
                        raise HTTPException(422, str(exc))
                    changes = [{"block": i, "data": target[i*4:i*4+4].hex().upper()} for i in range(8, min(40, len(memory)//4)) if target[i*4:i*4+4] != memory[i*4:i*4+4]]
                    job.update(backup_id=original["id"], memory_hex=target.hex(), write_blocks=changes)
                locked = baseline.get("locks", [])
                if any(w["block"] < len(locked) and locked[w["block"]] for w in job["write_blocks"]):
                    raise HTTPException(422, "The proposed write includes locked memory blocks")
                job.update(expected_memory_sha256=baseline["sha256"], baseline_backup_id=baseline["id"], confirmed_at=now())
            result = store.create("jobs", job)
            store.audit(f"Queued {mode} {kind}: {card['label']}")
            return result

    @app.post("/api/jobs/{ident}/cancel", dependencies=[Depends(session_user)])
    def cancel_job(ident: str):
        with store.lock:
            job = record("jobs", ident)
            if job["status"] != "queued":
                raise HTTPException(409, "Only queued jobs can be cancelled; an active hardware operation must finish")
            job.update(status="cancelled", updated_at=now())
            return store.put("jobs", job)

    @app.post("/api/bridge/heartbeat", dependencies=[Depends(bridge_user)])
    def heartbeat(payload: dict = Body(...)):
        if payload.get("mode") not in {"simulation", "hardware"}:
            raise HTTPException(422, "Unknown bridge mode")
        capabilities = payload.get("capabilities", [])
        if not isinstance(capabilities, list) or any(not isinstance(item, str) for item in capabilities):
            raise HTTPException(422, "Bridge capabilities must be a list of names")
        info = {k: payload.get(k) for k in ("name", "mode", "version", "port")}
        info["capabilities"] = [INITIALIZATION_CAPABILITY] if INITIALIZATION_CAPABILITY in capabilities else []
        store.put("system", {**info, "id": "bridge", "last_seen": now(), "last_seen_epoch": time.time()})
        return {"ok": True}

    @app.get("/api/bridge/jobs/next", dependencies=[Depends(bridge_user)])
    def next_job(kind: str | None = None):
        if kind is not None and kind not in {"read", "write", "restore", "initialize"}:
            raise HTTPException(422, "Unknown requested job kind")
        with store.lock:
            bridge = bridge_status()
            for job in reversed(store.all("jobs")):
                if job["status"] == "running":
                    return {"job": None}
                if job["status"] != "queued":
                    continue
                if kind is not None and job["kind"] != kind:
                    continue
                if job["kind"] == "initialize" and INITIALIZATION_CAPABILITY not in bridge.get("capabilities", []):
                    continue
                if job["expires_at"] < time.time() or job["mode"] != bridge["mode"]:
                    job.update(status="expired", error="Approval expired or bridge mode changed", updated_at=now())
                    store.put("jobs", job)
                    continue
                job.update(status="running", started_at=now(), updated_at=now())
                if job["mode"] == "hardware" and job["kind"] != "read":
                    card = record("cards", job["card_id"])
                    card.update(verified_at=None, verification="unverified")
                    store.put("cards", card)
                store.put("jobs", job)
                return {"job": job}
        return {"job": None}

    @app.post("/api/jobs/{ident}/resolve", dependencies=[Depends(session_user)])
    def resolve_interrupted_job(ident: str, payload: dict = Body(...)):
        with store.lock:
            job = record("jobs", ident)
            if job["status"] != "running" or bridge_status()["online"]:
                raise HTTPException(409, "Only an interrupted job with an offline bridge can be resolved")
            if payload.get("confirm_bridge_stopped") is not True or payload.get("confirm_label") != job["label"]:
                raise HTTPException(422, "Stop the Windows bridge and confirm the physical card label before resolving this job")
            job.update(status="failed", error="Interrupted operation; no automatic retry. Read this card again before another write.", updated_at=now())
            store.put("jobs", job)
            card = record("cards", job["card_id"])
            card["needs_fresh_read_modes"] = sorted(set(card.get("needs_fresh_read_modes", []) + [job["mode"]]))
            card["needs_fresh_read"] = True
            if job["kind"] == "initialize":
                mark_initialization_review(card, job["mode"], job["id"])
            if job["mode"] == "hardware":
                card.update(verified_at=None, verification="unverified")
            store.put("cards", card)
            store.audit(f"Interrupted job resolved: {card['label']}", "error")
            return job

    @app.post("/api/bridge/jobs/{ident}/backup", dependencies=[Depends(bridge_user)])
    def bridge_backup(ident: str, payload: dict = Body(...)):
        with store.lock:
            job = record("jobs", ident)
            if job["status"] != "running":
                raise HTTPException(409, "Job is not running")
            if payload.get("mode", job["mode"]) != job["mode"]:
                raise HTTPException(409, "Backup mode does not match the approved job")
            if job["mode"] == "hardware" and payload.get("simulated") is True:
                raise HTTPException(409, "A simulated backup cannot establish physical card evidence")
            parsed = parse_or_error(payload.get("dump"))
            if job["kind"] == "initialize":
                if payload.get("mode") != job["mode"]:
                    raise HTTPException(409, "Initialization backup mode does not match its approval")
                try:
                    validate_initialization_job(job, payload.get("dump"))
                except (ValueError, TypeError, KeyError) as exc:
                    raise HTTPException(409, str(exc))
            if job.get("expected_memory_sha256") and parsed["sha256"] != job["expected_memory_sha256"]:
                raise HTTPException(409, "Card contents changed since approval; read again and create a new approval")
            backup = save_backup(record("cards", job["card_id"]), payload["dump"], job["mode"], ident)
            job["prewrite_backup_id"] = backup["id"]
            store.put("jobs", job)
            return {"saved": True, "sha256": backup["sha256"], "backup_id": backup["id"]}

    @app.get("/api/bridge/jobs/{ident}", dependencies=[Depends(bridge_user)])
    def bridge_job(ident: str):
        return {"job": record("jobs", ident)}

    @app.post("/api/bridge/jobs/{ident}/result", dependencies=[Depends(bridge_user)])
    def bridge_result(ident: str, payload: dict = Body(...)):
        with store.lock:
            job = record("jobs", ident)
            result_digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if job["status"] != "running":
                if job.get("result_digest") == result_digest:
                    return {"ok": True, "job": job, "already_completed": True}
                raise HTTPException(409, "Job is not running")
            success = payload.get("success") is True
            error = str(payload.get("error") or "")[:2000]
            if payload.get("mode") != job["mode"]:
                success, error = False, "Bridge result mode does not match approved job"
            if job["mode"] == "hardware" and payload.get("simulated") is True:
                success, error = False, "A simulated result cannot verify a physical card"
            if job["kind"] == "initialize" and payload.get("initialization_identity_readback_dump") is not None:
                try:
                    initialization_failure_evidence(record("cards", job["card_id"]),
                        payload["initialization_identity_readback_dump"], job, stage="initialization_identity_readback")
                except (ValueError, TypeError, KeyError, HTTPException, OSError) as exc:
                    job["initialization_identity_evidence_error"] = str(getattr(exc, "detail", exc))[:1000]
            parsed = None
            if success:
                dump = payload.get("readback_dump") or payload.get("backup_dump")
                try:
                    parsed = parse_dump(dump)
                    if job["kind"] == "initialize":
                        validate_initialization_readback(job, dump)
                    elif job.get("uid") and parsed["uid"] != job["uid"]:
                        raise ValueError("Readback UID does not match approved card")
                    if job["kind"] != "read":
                        if not job.get("prewrite_backup_id"):
                            raise ValueError("Original contents were not backed up before the write")
                        if bytes.fromhex(parsed["memory_hex"]) != bytes.fromhex(job["memory_hex"]):
                            raise ValueError("Readback does not match the approved card image")
                    saved_card = record("cards", job["card_id"])
                    if job["kind"] == "initialize":
                        if saved_card.get("uid") != job["original_uid"]:
                            raise ValueError("Inventory identity changed before initialization verification")
                        # This narrowly scoped adoption follows full UID/memory/
                        # lock/metadata verification. save_backup's ordinary UID
                        # matching rule remains unchanged for all other paths.
                        saved_card = {**saved_card, "uid": job["target_uid"], "original_uid": job["original_uid"],
                            "initialization_job_id": ident, "template_backup_id": job["template_backup_id"],
                            "initialization_mode": job["mode"],
                            "hidden_configuration_verified": False, "offline_acceptance": "not_tested"}
                    save_backup(saved_card, dump, job["mode"], ident)
                    if job["kind"] == "initialize":
                        store.audit(f"Verified observable initialization identity for {saved_card['label']}: {job['original_uid']} → {job['target_uid']}; special configuration is not readback-verified")
                except (ValueError, TypeError, KeyError, HTTPException, OSError) as exc:
                    success, error = False, f"Verification failed: {getattr(exc, 'detail', str(exc))}"
            if not success and payload.get("readback_dump"):
                try:
                    evidence_card = record("cards", job["card_id"])
                    if job["kind"] == "initialize" or manual_initialization_review(evidence_card, job["mode"]):
                        initialization_failure_evidence(evidence_card, payload["readback_dump"], job)
                    else:
                        evidence = save_backup(evidence_card, payload["readback_dump"], f"failed-readback-{job['mode']}", ident)
                        job["failed_readback_backup_id"] = evidence["id"]
                except (ValueError, TypeError, KeyError, HTTPException, OSError) as exc:
                    job["evidence_error"] = str(getattr(exc, "detail", exc))[:1000]
            if job["kind"] == "initialize":
                job.update(initialization_attempted=payload.get("initialization_attempted") is True,
                    manual_reconciliation_required=not success, ordinary_memory_verified=success,
                    initialized=success, hidden_configuration_preservation_verified=False,
                    machine_acceptance_verified=False)
            job.update(status="succeeded" if success else "failed", error=error or None,
                completed_at=now(), updated_at=now(), hardware_verified=success and job["mode"] == "hardware",
                simulated=job["mode"] == "simulation", result_digest=result_digest)
            store.put("jobs", job)
            card = record("cards", job["card_id"])
            if job["kind"] == "initialize" and not success:
                mark_initialization_review(card, job["mode"], job["id"])
            if job["kind"] != "read":
                if success and job["mode"] == "hardware":
                    card.update(recipe_id=job.get("recipe_id"), recipe_version=job.get("recipe_version"), verified_at=now(), verification="verified", verified_memory_sha256=parsed["sha256"])
                elif job["mode"] == "hardware":
                    card.update(verified_at=None, verification="unverified", needs_fresh_read=True)
                    card["needs_fresh_read_modes"] = sorted(set(card.get("needs_fresh_read_modes", []) + ["hardware"]))
                else:
                    card["simulation_status"] = job["status"]
                    if not success:
                        card["needs_fresh_read_modes"] = sorted(set(card.get("needs_fresh_read_modes", []) + ["simulation"]))
                        card["needs_fresh_read"] = True
                store.put("cards", card)
            elif success:
                card["needs_fresh_read_modes"] = [mode for mode in card.get("needs_fresh_read_modes", [])
                    if mode != job["mode"] or manual_initialization_review(card, job["mode"])]
                card["needs_fresh_read"] = bool(card["needs_fresh_read_modes"])
                if job["mode"] == "hardware" and card.get("verified_memory_sha256") != parsed["sha256"]:
                    card.update(verified_at=None, verification="unverified", recipe_id=None, recipe_version=None)
                store.put("cards", card)
            store.audit(f"{job['mode'].title()} {job['kind']} {job['status']}: {card['label']}", "success" if success else "error")
            return {"ok": True, "job": job}

    @app.get("/api/backup", dependencies=[Depends(session_user)])
    def full_backup():
        import sqlite3
        import tempfile
        output = io.BytesIO()
        with store.lock, tempfile.TemporaryDirectory() as temporary:
            snapshot = Path(temporary) / "xbloom.sqlite3"
            with store.connect() as live, closing(sqlite3.connect(snapshot)) as copied:
                live.backup(copied)
                copied.execute("DELETE FROM sessions")
                copied.commit()
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                files = {"xbloom.sqlite3": snapshot}
                files.update({str(p.relative_to(store.root)).replace("\\", "/"): p for p in store.root.rglob("*")
                              if p.is_file() and p.name not in {"xbloom.sqlite3", "xbloom.sqlite3-wal", "xbloom.sqlite3-shm"} and not p.name.endswith(".tmp")})
                manifest = {"schema_version": 1, "app_version": __version__, "created_at": now(), "files": {}}
                for name, path in files.items():
                    data = path.read_bytes()
                    archive.writestr(name, data)
                    manifest["files"][name] = hashlib.sha256(data).hexdigest()
                archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        output.seek(0)
        return StreamingResponse(output, media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="xbloom-backup.zip"'})

    static = Path(__file__).parent / "static"
    if static.exists():
        app.mount("/static", StaticFiles(directory=static), name="static")

        @app.get("/")
        def index():
            return FileResponse(static / "index.html", headers={"Cache-Control": "no-cache"})
    return app


app = create_app()
