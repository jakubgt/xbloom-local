from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.models import Recipe
from app.nfc import decode_recipe, parse_dump, preview_recipe
from app.preferences import BrewingDefaults, apply_brewing_defaults, brewing_defaults_view
from bridge.main import synthetic_dump


def test_moving_v60_setup_encodes_other_and_automatic_grinding_at_56_and_80_rpm():
    view = brewing_defaults_view({"equipment_profile": "v60_01_melodrip"})
    recipe = Recipe.model_validate({"name": "Synthetic external dripper", **view["recipe_defaults"]})
    memory = bytes.fromhex(parse_dump(synthetic_dump("E000000000000011"))["memory_hex"])
    preview = preview_recipe(recipe.model_dump(), memory)
    assert preview["compatible"]
    assert preview["differences"] == []
    assert preview["effective"]["cup_type"] == "other"
    assert view["grinder_enabled"] is True
    assert preview["effective"]["grinder_enabled"] is True
    decoded = decode_recipe(bytes.fromhex(preview["memory_hex"]))
    assert decoded["checksum_valid"] is True
    assert decoded["cup_type"] == "other"
    assert decoded["grinder_enabled"] is True
    assert decoded["grind_setting"] == 56
    assert decoded["grinder_rpm"] == 80
    assert decoded["dose_g"] == 18
    assert sum(p.volume_ml for p in recipe.pours) == 288
    assert [(p.volume_ml, p.temperature_c, p.flow_ml_s, p.pattern, p.pause_s) for p in recipe.pours] == [
        (54, 94, 3.5, "spiral", 35), (72, 93, 3.5, "center", 15),
        (72, 92, 3.5, "center", 15), (90, 92, 3.5, "center", 0)]
    assert all(not p.agitation_before and not p.agitation_after for p in recipe.pours)
    assert "moving adapter" in recipe.notes
    assert "automatically grind directly into the V60" in recipe.notes
    assert "clear of adapter and spout movement" in recipe.notes
    assert "after the bloom" in recipe.notes
    assert "Separately grind" not in recipe.notes and "transfer the grounds" not in recipe.notes
    assert "not automated" in recipe.notes
    assert "cannot sense" in recipe.notes


def test_preferences_are_copies_and_do_not_rewrite_existing_inputs():
    old = {"name": "Existing recommendation", "cup_type": "omni", "notes": "Bean-specific reasoning."}
    original = deepcopy(old)
    result = apply_brewing_defaults(old, {"equipment_profile": "v60_01_melodrip"})
    assert old == original
    assert result["name"] == old["name"]
    assert result["notes"].startswith("Bean-specific reasoning.")
    assert apply_brewing_defaults(old) == old
    result["pours"][0]["volume_ml"] = 1
    assert brewing_defaults_view({"equipment_profile": "v60_01_melodrip"})["recipe_defaults"]["pours"][0]["volume_ml"] == 54


def test_invalid_or_unrelated_preferences_are_rejected():
    for payload in ({"equipment_profile": "unknown"}, {"allow_writes": True}):
        with pytest.raises(ValidationError):
            BrewingDefaults.model_validate(payload)


def test_saved_preferences_apply_to_new_lookup_only_and_survive_restart(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as main
    monkeypatch.setenv("XBLOOM_AUTH_MODE", "trusted_lan")
    app = main.create_app(tmp_path)
    seed = {"name": "Synthetic old recipe", **brewing_defaults_view()["recipe_defaults"], "grinder_enabled": False}
    lookup = {"profile": {"schema_version": 1, "bean": {"name": "Synthetic"}, "recipe": deepcopy(seed)}, "warnings": []}
    monkeypatch.setattr(main, "lookup_product", lambda url: deepcopy(lookup))
    with TestClient(app) as client:
        client.get("/api/session")
        original = client.post("/api/recipes", json=seed).json()
        changed = client.patch("/api/settings/brewing-defaults", json={"equipment_profile": "v60_01_melodrip"})
        assert changed.status_code == 200, changed.text
        assert changed.json()["grinder_enabled"] is True
        assert changed.json()["recipe_defaults"]["grinder_enabled"] is True
        fetched = client.post("/api/import/from-url", json={"url": "https://synthetic.example/coffee"}).json()
        assert fetched["profile"]["recipe"]["cup_type"] == "other"
        assert fetched["profile"]["recipe"]["grinder_enabled"] is True
        assert fetched["profile"]["recipe"]["grind_setting"] == 56
        assert fetched["profile"]["recipe"]["grinder_rpm"] == 80
        assert client.get("/api/recipes/" + original["id"]).json() == original
        assert original["grinder_enabled"] is False
        assert len(client.get("/api/recipes").json()["items"]) == 1
        # Exact imported profiles remain explicit; preferences never silently alter one.
        preview = client.post("/api/import/preview", json={"profile": lookup["profile"]}).json()
        assert preview["recipe"]["cup_type"] == "omni"
        assert preview["recipe"]["grinder_enabled"] is False
        assert client.patch("/api/settings/brewing-defaults", json={}).status_code == 422
        assert client.patch("/api/settings/brewing-defaults", json={"equipment_profile": "invalid"}).status_code == 422
        assert client.patch("/api/settings/brewing-defaults", json={"equipment_profile": "omni"}, headers={"Origin": "https://evil.example"}).status_code == 403
    with TestClient(main.create_app(tmp_path)) as client:
        client.get("/api/session")
        saved = client.get("/api/settings").json()["brewing_defaults"]
        assert saved["equipment_profile"] == "v60_01_melodrip"
        assert saved["grinder_enabled"] is True
        assert saved["recipe_defaults"]["grinder_enabled"] is True
        assert client.get("/api/settings/brewing-defaults").json() == saved
        fetched = client.post("/api/import/from-url", json={"url": "https://synthetic.example/coffee"}).json()
        assert fetched["profile"]["recipe"]["grinder_enabled"] is True
        assert client.get("/api/recipes/" + original["id"]).json() == original
