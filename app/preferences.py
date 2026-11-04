"""Saved equipment preferences for new recommendations, never existing recipes."""
from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict


class BrewingDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")
    equipment_profile: Literal["omni", "v60_01_melodrip"] = "omni"


V60_INSTRUCTIONS = (
    "Equipment: Hario V60 01 Clear with a rinsed size 01 cone filter and a handheld Melodrip. "
    "Use your moving adapter to carry the V60 to the grinder, then into brewing position above a stable cup/server. "
    "Load the prescribed 18 g bean dose and let the xBloom automatically grind directly into the V60 "
    "at the recipe's grind setting and RPM before brewing. Automatic card grinding is enabled. "
    "Check that the complete setup clears the moving spout, and keep the handheld Melodrip clear of adapter and spout movement. "
    "Bloom directly without the Melodrip; gently wet any dry pockets during the bloom pause. "
    "Position the Melodrip beneath the centered spout after the bloom, hold it by its handle over the bed, "
    "and use it for the remaining pours. Its placement and any gentle manual bloom stirring are not automated. "
    "The card uses fixed pauses; it cannot sense the water level recommended in the Melodrip method. "
    "Observe the first brew and pause dispensing if water accumulates too high. "
    "This is an adapted starting recommendation, not a validated Hario, Melodrip or roaster recipe."
)


def brewing_defaults_view(value=None):
    checked = BrewingDefaults.model_validate(value or {}).model_dump()
    v60 = checked["equipment_profile"] == "v60_01_melodrip"
    recipe = {
        "dose_g": 18, "ratio": 16, "total_water_ml": 288,
        "grind_setting": 56 if v60 else 55, "grinder_rpm": 80,
        "grinder_enabled": True, "cup_type": "other" if v60 else "omni",
        "dripper": "Hario V60 01 Clear" if v60 else "xBloom Omni Dripper 2",
        "filter": "V60 size 01 paper filter, rinsed" if v60 else "Omni paper filter, rinsed",
        "notes": V60_INSTRUCTIONS if v60 else "Starting recommendation; adjust after tasting.",
        "pours": [
            {"volume_ml": volume, "temperature_c": temperature, "flow_ml_s": 3.5 if v60 else 3.0,
             "pattern": pattern, "pause_s": pause, "agitation_before": False, "agitation_after": False}
            for volume, temperature, pattern, pause in (
                ((54, 94, "spiral", 35), (72, 93, "center", 15), (72, 92, "center", 15), (90, 92, "center", 0))
                if v60 else
                ((54, 92, "circle", 35), (78, 91, "spiral", 15), (78, 90, "spiral", 15), (78, 90, "center", 0))
            )
        ],
    }
    return {
        **checked, "label": "Hario V60 01 Clear + Melodrip" if v60 else "xBloom Omni Dripper 2",
        "cup_type": recipe["cup_type"], "dripper": recipe["dripper"], "filter": recipe["filter"],
        "accessory": "Melodrip (manual placement)" if v60 else None,
        "grinder_enabled": recipe["grinder_enabled"],
        "manual_instructions": V60_INSTRUCTIONS if v60 else "",
        "recipe_defaults": recipe,
    }


def apply_brewing_defaults(recipe, value=None):
    """Apply equipment to a newly generated recommendation, without mutating input."""
    preferences = brewing_defaults_view(value)
    result = deepcopy(recipe)
    if preferences["equipment_profile"] == "v60_01_melodrip":
        existing_notes = result.get("notes", "")
        result.update(deepcopy(preferences["recipe_defaults"]))
        result["notes"] = (existing_notes + "\n\n" + V60_INSTRUCTIONS).strip()
    return result
