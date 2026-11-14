"""Plain-language reading of a field's crop history: groups, rotation, this year's likely crop.

The crop maps name classes differently in each country ("Corn for grain" in Canada,
"Corn" in the US) and split crops finely. Every class is put in one of a few groups a
farmer would use, and the history is read in those groups.

The rotation is found by checking whether the recent years repeat every one, two or
three years (a stable crop, an alternation, or a three-year rotation). When one fits,
the crop expected this year is the one grown a period ago. This is an estimate from the
pattern only: the newest map is always last season, and farmers change plans.
"""
from __future__ import annotations

import re
from datetime import date

# key, label used in sentences, is a crop
GROUPS = {
    "corn": ("Corn", True),
    "soybeans": ("Soybeans", True),
    "wheat": ("Wheat", True),
    "other_grains": ("Other grains", True),
    "canola": ("Canola", True),
    "pulses": ("Beans", True),
    "hay": ("Hay", True),
    "pasture": ("Pasture", True),
    "fallow": ("Fallow", True),
    "vegetables": ("Vegetables", True),
    "fruit": ("Fruit", True),
    "other_crops": ("Other crops", True),
    "natural": ("Woodland or wetland", False),
    "water": ("Water", False),
    "developed": ("Developed land", False),
    "barren": ("Bare land", False),
}

_RULES = (  # first match wins; order matters ("Dbl Crop WinWht/Soybeans" is wheat first)
    (r"dbl crop winwht|winter wheat|spring wheat|durum|^wheat", "wheat"),
    (r"corn", "corn"),
    (r"soybean", "soybeans"),
    (r"canola|rapeseed|mustard|flax|sunflower|safflower|camelina|borage|oilseed", "canola"),
    (r"wetland|peatland|forest|wood|shrub|coniferous|broadleaf|mixedwood|burnt", "natural"),
    (r"pasture|grassland", "pasture"),
    (r"\bpeas?\b|bean|lentil|chick ?pea|pulse|faba|vetch", "pulses"),
    (r"alfalfa|hay|clover|forage|sod|grass seed|switchgrass", "hay"),
    (r"grass", "pasture"),
    (r"fallow|idle|too wet", "fallow"),
    (r"barley|oats|rye|triticale|spelt|millet|sorghum|buckwheat|canaryseed|quinoa|cereal|grain|rice", "other_grains"),
    (r"potato|tomato|vegetable|sugarbeet|pepper|onion|cucumber|squash|pumpkin|lettuce|carrot|melon|cantaloupe|cabbage|"
     r"broccoli|tobacco|ginseng|hops|herb|hemp|mint|garlic|asparagus|greenhouse|nursery|cotton|peanut", "vegetables"),
    (r"apple|cherr|peach|pear|grape|vineyard|orchard|berr|citrus|orange|almond|walnut|pecan|pistachio|olive|plum|"
     r"apricot|fruit|nut", "fruit"),
    (r"water|aquaculture", "water"),
    (r"developed|urban", "developed"),
    (r"barren|exposed|ice|snow", "barren"),
)

RECENT_YEARS = 9  # years read for the pattern
MIN_SHARE = 0.5  # a year counts towards the pattern when its main class covers at least half the field
MERGED_PURITY = 0.6  # years under this share suggest the outline covers more than one field
NOT_CROPLAND = 0.5  # recent crop share under this means mostly grass, trees or buildings
STABLE_SHARE = 0.75  # one group in at least this share of recent years reads as "mostly" that crop
ROTATION_FIT = 0.75  # share of years that repeat the year one period earlier


def group_of(name: str | None, is_crop: bool | None = None) -> str:
    """Group key for a crop map class name."""
    low = (name or "").lower()
    for pattern, key in _RULES:
        if re.search(pattern, low):
            return key
    return "other_crops" if is_crop is not False else "natural"


def label(key: str) -> str:
    return GROUPS.get(key, (key, True))[0]


def _sequence(rows: list[dict]) -> list[tuple[int, str]]:
    """(year, group) for recent years whose main class is clear enough to read."""
    rows = sorted(rows, key=lambda r: r["year"])[-RECENT_YEARS:]
    return [(r["year"], r["group"]) for r in rows if r.get("share", 0) >= MIN_SHARE]


def _period(groups: list[str]) -> tuple[int, float, bool, str | None] | None:
    """The pattern that fits the recent years: (period, fit, still holds last season, main group).

    Period 1 means one group dominates (at least STABLE_SHARE of the years). Periods 2 and 3
    are rotations of at least two groups in which most years repeat the year one period
    earlier. "Still holds" is False when last season broke the pattern.
    """
    if len(groups) >= 3:
        mode = max(set(groups), key=groups.count)
        share = groups.count(mode) / len(groups)
        if share >= STABLE_SHARE:
            return 1, share, groups[-1] == mode, mode
    for p in (2, 3):
        if len(groups) < 2 * p + 1:
            continue
        checks = [groups[i] == groups[i - p] for i in range(p, len(groups))]
        fit = sum(checks) / len(checks)
        window = groups[-(p + 1):-1] if not checks[-1] else groups[-p:]
        if fit >= ROTATION_FIT and len(set(window)) >= 2:
            return p, fit, checks[-1], None
    return None


def _join(names: list[str]) -> str:
    names = [n.lower() if i else n for i, n in enumerate(names)]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def summarize(rows: list[dict], today: date | None = None) -> dict:
    """Plain-language summary of a crop history for display and planning."""
    this_year = (today or date.today()).year
    rows = sorted(rows, key=lambda r: r["year"])
    if not rows:
        return {"yearsOfData": 0, "firstYear": None, "lastSeason": None, "pattern": None, "expectedThisYear": None,
                "flags": {"possiblyMerged": False, "mostlyNotCropland": False}, "notes": []}

    last = rows[-1]
    sequence = _sequence(rows)
    groups = [g for _, g in sequence]
    found = _period(groups) if groups else None

    pattern = None
    expected = None
    if found and not found[2]:
        # Last season broke the pattern: describe the change and do not guess.
        p, _, _, mode = found
        before, last_year = sequence[-2][0], sequence[-1][0]
        earlier = f"Mostly {label(mode).lower()}" if p == 1 else "A regular rotation"
        text = f"{earlier} until {before}, then {label(groups[-1]).lower()} in {last_year}."
        pattern = {"kind": "changed", "periodYears": p, "groups": list(dict.fromkeys(groups)), "text": text}
    elif found:
        p, fit, _, mode = found
        cycle = [mode] if p == 1 else groups[-p:]
        names = [label(g) for g in dict.fromkeys(cycle)]
        if p == 1:
            kind, text = "stable", f"Mostly {names[0].lower()} in recent years."
        elif p == 2:
            kind, text = "alternating", f"{_join(names)} take turns, one year each."
        else:
            steps = [label(g).lower() for g in cycle]
            kind, text = "rotation", f"A {p}-year rotation: {', '.join(steps[:-1])}, then {steps[-1]}."
        pattern = {"kind": kind, "periodYears": p, "groups": list(dict.fromkeys(cycle)), "text": text}
        last_year = sequence[-1][0]
        gap = this_year - last_year
        guess = cycle[(gap - 1) % p] if gap >= 1 else None
        if guess and gap <= 2 and GROUPS.get(guess, ("", False))[1]:
            confident = fit == 1.0 and len(groups) >= 2 * p + 2
            reason = (f"{text} {label(groups[-1])} in {last_year}, so the pattern points to {label(guess).lower()} this year."
                      if p > 1 else f"{text} The pattern points to {label(guess).lower()} again this year.")
            expected = {"year": this_year, "group": guess, "crop": label(guess),
                        "confidence": "high" if confident else "medium", "reason": reason}
    elif groups:
        counts: dict[str, int] = {}
        for g in groups:
            counts[g] = counts.get(g, 0) + 1
        top = [label(g) for g, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:3]]
        pattern = {"kind": "mixed", "periodYears": None, "groups": list(counts), "text": f"No regular pattern: mostly {_join(top).lower()}."}

    shares = [r["share"] for r in rows if r.get("share") is not None]
    possibly_merged = bool(shares) and sum(s < MERGED_PURITY for s in shares) >= max(3, len(shares) / 2)
    recent = [r["cropShare"] for r in rows[-5:] if r.get("cropShare") is not None]
    mostly_not_cropland = bool(recent) and sum(recent) / len(recent) < NOT_CROPLAND

    notes = [f"The newest crop map is {last['year']} (last season); {this_year} is not mapped yet."]
    if possibly_merged:
        notes.append("Many years show a mix of crops, so this outline may cover more than one field.")
    if mostly_not_cropland:
        notes.append("In recent years this area was mostly grass, trees or buildings rather than crops.")
    if pattern and pattern["kind"] == "changed":
        notes.append("The pattern changed last season, so this year's crop is not estimated.")
    elif expected is None and pattern and pattern["kind"] == "mixed":
        notes.append("The crops do not follow a clear pattern, so this year's crop is not estimated.")

    return {
        "yearsOfData": len(rows),
        "firstYear": rows[0]["year"],
        "lastSeason": {"year": last["year"], "crop": last["crop"], "group": last["group"], "groupLabel": label(last["group"])},
        "pattern": pattern,
        "expectedThisYear": expected,
        "flags": {"possiblyMerged": possibly_merged, "mostlyNotCropland": mostly_not_cropland},
        "notes": notes,
    }
