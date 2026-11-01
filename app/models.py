from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class Bean(Model):
    name: str = Field(min_length=1, max_length=160)
    roaster: str | None = Field(default=None, max_length=160)
    product_url: str | None = Field(default=None, max_length=2048)
    country: str | None = None
    region: str | None = None
    farm: str | None = None
    elevation: str | None = None
    process: str | None = None
    variety: str | None = None
    roast_level: str | None = None
    roast_date: str | None = None
    flavor_notes: list[str] = Field(default_factory=list, max_length=30)
    notes: str = Field(default="", max_length=10000)
    rating: int | None = Field(default=None, ge=1, le=5)
    provenance: dict[str, str] = Field(default_factory=dict)

    @field_validator("product_url")
    @classmethod
    def safe_url(cls, value):
        if value and not value.startswith(("https://", "http://")):
            raise ValueError("Product URL must start with https:// or http://")
        return value or None


class Pour(Model):
    volume_ml: float = Field(gt=0, le=500)
    temperature_c: float = Field(ge=20, le=100)
    flow_ml_s: float = Field(gt=0, le=10)
    pattern: Literal["center", "circle", "spiral"] = "circle"
    pause_s: float = Field(default=0, ge=0, le=300)
    agitation_before: bool = False
    agitation_after: bool = False


class Recipe(Model):
    name: str = Field(min_length=1, max_length=160)
    bean_id: str | None = None
    dose_g: float = Field(default=18, gt=0, le=100)
    ratio: float = Field(default=16, ge=1, le=100)
    total_water_ml: float = Field(default=288, gt=0, le=2000)
    grind_setting: float = Field(default=60, ge=0, le=200)
    grinder_rpm: int = Field(default=80, ge=0, le=300)
    grinder_enabled: bool = True
    cup_type: Literal["omni", "other", "xpod"] = "omni"
    xid: str | None = Field(default=None, max_length=7)
    pours: list[Pour] = Field(min_length=1, max_length=20)
    notes: str = Field(default="", max_length=10000)
    dripper: str | None = None
    filter: str | None = None
    water_profile: str | None = None
    recommendation: bool = True

    @model_validator(mode="after")
    def check_totals(self):
        if abs(sum(p.volume_ml for p in self.pours) - self.total_water_ml) > 0.1:
            raise ValueError("Pour volumes must add up to total water")
        if abs(self.dose_g * self.ratio - self.total_water_ml) > 0.2:
            raise ValueError("Dose × ratio must equal total water (within 0.2 ml)")
        return self


class Card(Model):
    label: str = Field(min_length=1, max_length=100)
    uid: str | None = None
    type: Literal["custom"] = "custom"
    notes: str = Field(default="", max_length=5000)

    @field_validator("uid")
    @classmethod
    def uid_value(cls, value):
        if not value:
            return None
        value = value.replace(" ", "").upper()
        if len(value) != 16 or any(c not in "0123456789ABCDEF" for c in value) or not value.startswith("E0"):
            raise ValueError("Use the 8-byte UID as displayed by Proxmark, beginning E0")
        return value


class Profile(Model):
    schema_version: Literal[1] = 1
    bean: Bean
    recipe: Recipe
