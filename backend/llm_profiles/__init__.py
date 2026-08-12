"""Optional provider-specific prompt profiles.

The estimator core stays provider-neutral. A profile may tighten an LLM's
output contract, but it must not change retrieval candidates, formulas,
variables, or calculation code.
"""

from __future__ import annotations

from .solar_pro2 import PROFILE_ID as SOLAR_PRO2_PROFILE_ID
from .solar_pro2 import dedicated_staff_value_prompt as solar_pro2_staff_prompt
from .solar_pro2 import normalize_response as normalize_solar_pro2_response
from .solar_pro2 import research_direct_prompt as solar_pro2_research_prompt
from .solar_pro2 import system_instruction as solar_pro2_system_instruction
from .solar_pro2 import (
    use_unique_prequalified_organization_value as solar_pro2_use_unique_value,
)


def system_instruction(profile: str, prompt: str) -> str:
    if profile == SOLAR_PRO2_PROFILE_ID:
        return solar_pro2_system_instruction(prompt)
    return ""


def normalize_response(profile: str, prompt: str, payload: object) -> object:
    if profile == SOLAR_PRO2_PROFILE_ID:
        return normalize_solar_pro2_response(prompt, payload)
    return payload


def use_unique_prequalified_organization_value(
    profile: str,
    *,
    candidate_count: int,
    target_organization_form: str,
) -> bool:
    if profile == SOLAR_PRO2_PROFILE_ID:
        return solar_pro2_use_unique_value(
            candidate_count=candidate_count,
            target_organization_form=target_organization_form,
        )
    return False


def dedicated_staff_value_prompt(
    profile: str,
    *,
    target_events: list[dict],
    candidates: list[dict],
) -> str | None:
    if profile == SOLAR_PRO2_PROFILE_ID:
        return solar_pro2_staff_prompt(
            target_events=target_events,
            candidates=candidates,
        )
    return None


def research_direct_prompt(
    profile: str,
    *,
    event: dict,
    source_text: str,
    candidates: list[dict],
) -> tuple[str, list[dict]] | None:
    if profile == SOLAR_PRO2_PROFILE_ID:
        return solar_pro2_research_prompt(
            event=event,
            source_text=source_text,
            candidates=candidates,
        )
    return None


__all__ = [
    "SOLAR_PRO2_PROFILE_ID",
    "dedicated_staff_value_prompt",
    "normalize_response",
    "research_direct_prompt",
    "system_instruction",
    "use_unique_prequalified_organization_value",
]
