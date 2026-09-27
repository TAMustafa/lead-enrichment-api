"""Evidence-based cuisine classification from Google type identifiers, never alphabetical rank."""

import json
from functools import lru_cache
from pathlib import Path

DEFAULT_RULES = Path(__file__).with_name("cuisine-rules.json")


@lru_cache(maxsize=8)
def load_rules(path: Path) -> dict:
    rules = json.loads(path.read_text())
    if set(rules) != {"primary", "secondary", "umbrella"}:
        raise ValueError("Cuisine rules require primary, secondary and umbrella groups")
    for group in (rules["primary"], rules["secondary"]):
        if not isinstance(group, dict):
            raise ValueError("Cuisine group must map labels to type identifiers")
        for label, signals in group.items():
            if (
                not label
                or not isinstance(signals, list)
                or not signals
                or any(not isinstance(signal, str) or not signal for signal in signals)
            ):
                raise ValueError("Cuisine labels require explicit type signals")
    for label, rule in rules["umbrella"].items():
        if (
            not label
            or set(rule) != {"signals", "members"}
            or not rule["signals"]
            or not rule["members"]
            or any(not isinstance(value, str) or not value for value in rule["signals"] + rule["members"])
        ):
            raise ValueError("Cuisine umbrella rules require signals and member labels")
    return rules


def classify_cuisine(types: list[str], rules_path: Path = DEFAULT_RULES) -> dict:
    rules = load_rules(rules_path)
    observed = set(types)
    primary = {label for label, signals in rules["primary"].items() if observed.intersection(signals)}
    secondary = {label for label, signals in rules["secondary"].items() if observed.intersection(signals)}
    umbrella = {
        label
        for label, rule in rules["umbrella"].items()
        if observed.intersection(rule["signals"]) and primary <= set(rule["members"])
    }
    used_umbrella = len(primary) > 1 and len(umbrella) == 1
    primary_value = None
    if used_umbrella:
        primary_value = next(iter(umbrella))
    elif len(primary) == 1:
        primary_value = next(iter(primary))
    all_signals = {
        signal
        for group in (rules["primary"], rules["secondary"])
        for signals in group.values()
        for signal in signals
    } | {signal for rule in rules["umbrella"].values() for signal in rule["signals"]}
    evidence = sorted(observed.intersection(all_signals))
    return {
        "primary_cuisine": primary_value,
        "secondary_cuisine": next(iter(secondary)) if len(secondary) == 1 else None,
        "cuisine_evidence": evidence,
        "cuisine_reason_codes": (
            (
                ["PRIMARY_CUISINE_UMBRELLA_MATCHED"]
                if used_umbrella
                else ["PRIMARY_CUISINE_AMBIGUOUS"]
                if len(primary) > 1
                else ["PRIMARY_CUISINE_UNKNOWN"]
                if not primary
                else []
            )
            + (
                ["SECONDARY_CUISINE_AMBIGUOUS"]
                if len(secondary) > 1
                else ["SECONDARY_CUISINE_UNKNOWN"]
                if not secondary
                else []
            )
            or ["CUISINE_TYPE_EVIDENCE_MATCHED"]
        ),
    }
