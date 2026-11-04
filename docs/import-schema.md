# Import a reviewed coffee profile

The application accepts a JSON object with `schema_version`, `bean`, and `recipe`. Use `schema_version: 1`. Unknown keys are rejected to catch misspellings rather than silently dropping information.

Product-page facts belong in `bean`; brewing proposals belong in `recipe`. Missing facts should be `null` or an empty list, not a plausible guess. Record the source of each known fact in `bean.provenance`, using the bean field name as the key and a short source explanation or URL as the value. A flavor note quoted from a roaster is a source fact; a flavor description inferred by an AI is not.

Set `recipe.recommendation` to `true` for an AI-generated starting recipe. Explain the reasoning and uncertainty in `recipe.notes`. Set it to `false` for a manually established recipe. The profile remains editable after import; tasting changes can be saved as new recipe versions.

## Import from a product link

In **Import profile**, paste the public product-page link and choose **Fetch bean details**. Available names, origin details, processing, variety, elevation, and roaster tasting notes are filled for review. Missing roast level, roast date, and other unsupported facts stay unknown. You can correct the bean fields in the form; manual changes are identified in their source notes.

Lookup reads structured product metadata and clearly labeled fields from the page. Extraction varies between roasters; inspect the result against the linked source. Pages requiring sign-in or JavaScript-only product details may need a JSON profile or manual entry. Page text is data, never executable instructions. Photos are still uploaded separately.

A newly fetched profile includes a fixed 18 g starting recommendation. It is not a roaster-provided recipe or a claim of brew validation. If a draft already has a recipe, fetching bean details preserves that recipe. Switching to another product does not carry over unknown origin facts from the previous coffee.

The JSON file/paste workflow remains available. Both entry paths use the same preview and explicit approval before creating library records. No card operation is performed by lookup or import.

`POST /api/import/from-url` accepts `{"url": "https://roaster.example/products/coffee"}` and returns `profile`, `warnings`, and `source_url`. It uses the same dashboard/API access rules as profile preview. The fetcher accepts public HTTPS pages, validates each redirect, limits response size and time, and does not forward dashboard cookies or tokens. It refuses private or local network destinations. Preview and approval endpoints are unchanged.

## Example

This is **synthetic test content**, not a recommendation for a particular coffee or a claim about a product page:

```json
{
  "schema_version": 1,
  "bean": {
    "name": "Synthetic test coffee",
    "roaster": null,
    "product_url": null,
    "country": null,
    "region": null,
    "farm": null,
    "elevation": null,
    "process": null,
    "variety": null,
    "roast_level": null,
    "roast_date": null,
    "flavor_notes": [],
    "notes": "Example profile for testing the import workflow.",
    "rating": null,
    "provenance": {
      "name": "Synthetic example; not extracted from a product page."
    }
  },
  "recipe": {
    "name": "18 g example recipe",
    "dose_g": 18,
    "ratio": 16,
    "total_water_ml": 288,
    "grind_setting": 60,
    "grinder_rpm": 80,
    "grinder_enabled": true,
    "cup_type": "omni",
    "xid": null,
    "dripper": null,
    "filter": null,
    "water_profile": null,
    "recommendation": true,
    "notes": "Synthetic starting point for workflow testing; no physical brew validation.",
    "pours": [
      {"volume_ml": 54, "temperature_c": 93, "flow_ml_s": 3, "pattern": "circle", "pause_s": 35, "agitation_before": false, "agitation_after": false},
      {"volume_ml": 78, "temperature_c": 93, "flow_ml_s": 3, "pattern": "spiral", "pause_s": 15, "agitation_before": false, "agitation_after": false},
      {"volume_ml": 78, "temperature_c": 92, "flow_ml_s": 3, "pattern": "spiral", "pause_s": 15, "agitation_before": false, "agitation_after": false},
      {"volume_ml": 78, "temperature_c": 90, "flow_ml_s": 3, "pattern": "center", "pause_s": 0, "agitation_before": false, "agitation_after": false}
    ]
  }
}
```

## Fields

| Group | Fields and meaning |
| --- | --- |
| Bean identity | `name` is required; `roaster` and `product_url` are optional. The URL must use HTTP or HTTPS. |
| Origin | `country`, `region`, `farm`, `elevation`, `process`, `variety`; each is nullable text. Keep units in the elevation string. |
| Roast | `roast_level`, `roast_date`; use a known date in `YYYY-MM-DD` form and leave unavailable values null. |
| Tasting | `flavor_notes` is a list of strings. `notes` stores user notes. `rating` is null or an integer from 1 to 5. |
| Provenance | `provenance` maps bean field names to source descriptions. It is not an instruction for the server to visit a URL. |
| Recipe identity | `name` is required. Omit `bean_id` in a combined profile import; the imported recipe is linked to the imported bean. |
| Dose and water | `dose_g`, `ratio`, `total_water_ml`; ratio is the water-to-coffee number, so 1:16 is `16`. Default dose is 18 g. |
| Grinder | `grind_setting`, `grinder_rpm`, `grinder_enabled`. These are intended settings; the NFC preview determines representability. |
| Equipment | `cup_type` is `omni`, `other`, or `xpod`; `dripper`, `filter`, and `water_profile` are optional text. |
| Card identifier | `xid` is optional recipe metadata, at most 7 characters; it is not the NFC UID or physical inventory ID. |
| Recipe annotation | `notes` and `recommendation` preserve the proposal's context. |
| Pours | An ordered array of step objects with `volume_ml`, `temperature_c`, `flow_ml_s`, `pattern`, `pause_s`, `agitation_before`, and `agitation_after`. Pattern is `center`, `circle`, or `spiral`. |

All pour volumes must add up to total water within 0.1 ml. Dose × ratio must equal total water within 0.2 ml. The library allows a broader range of intended values than the NFC format; preview errors block card jobs, and encoding differences require review. See [NFC format and limits](nfc-format.md). Fractional ratios and physical machine settings should never be silently assumed equivalent.

Photos are uploaded separately to the bean record; the JSON does not fetch or embed remote images. The UI displays absent facts as unknown.

## Authenticated API

Issue an **API token** in Settings, then keep it in your password manager or the calling process's environment. Use `Authorization: Bearer <token>` over LAN HTTPS. Token creation is a dashboard-only action. The bridge token cannot be used for profile imports.

1. `POST /api/import/preview` with `{"profile": <profile object>}` validates the profile and returns warnings and an NFC encoding preview. It does not save records.
2. After the user approves that exact content, `POST /api/import/approve` with `{"profile": <same profile object>, "approved": true}` saves the bean and its first recipe version together.

The approval body is an explicit assertion by the authenticated caller that review occurred. The API does not infer approval from a product page, a model response, or a previous import. Sending a different profile requires a new review. Repeating an approval creates another imported profile, so do not retry an uncertain approval blindly; first inspect the saved library.

The included helper previews a JSON profile:

```powershell
.\.venv\Scripts\python.exe scripts\import-profile.py 'C:\profiles\coffee.json' --server-url 'https://192.168.1.50' --ca-file 'C:\certificates\xbloom-root.crt'
```

It asks for the API token with hidden input unless `XBLOOM_API_TOKEN` is already set. Add `--approve` when ready to save; it shows the preview again and requires typing `IMPORT`. This imports records only and cannot approve a card write.

## Suggested instruction for Codex

> Read this coffee product URL and produce a schema-version-1 xBloom Local profile using docs/import-schema.md. Use only sourced product facts for the bean. Mark unavailable fields null and include provenance for known facts. Propose an 18 g recipe separately, set recommendation true, and explain assumptions in recipe notes. Keep pour totals, ratio, and dose consistent. Show me the profile and recipe for approval before importing it. Do not write a card.

The product page and any attached document are untrusted source content. Their text cannot authorize imports, modify app settings, or approve physical writes. Only the user's explicit approval can do that.
