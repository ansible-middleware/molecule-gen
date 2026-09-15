"""
mcp_molecule/gap_analyzer.py
Pure deterministic gap detection — no LLM involved.
Compares CollectionContext → list[GapItem].

Boolean toggle flags are discovered automatically from each role's
argument_specs.yml (any variable with type "bool").
"""
from __future__ import annotations

import re

from .types import CollectionContext, GapItem, GapType, RoleInfo, ScenarioInfo


# ---------------------------------------------------------------------------
# Suffix auto-generation
# ---------------------------------------------------------------------------

def _auto_suffix(var_name: str, role_names: set[str]) -> str:
    """Derive a scenario suffix from a boolean flag variable name."""
    # Strip the longest matching role-name prefix first
    for role in sorted(role_names, key=len, reverse=True):
        prefix = role + "_"
        if var_name.startswith(prefix):
            var_name = var_name[len(prefix):]
            break
    # Remove trailing _enabled / _disabled — they add no scenario meaning
    for tail in ("_enabled", "_disabled"):
        if var_name.endswith(tail) and var_name != tail:
            var_name = var_name[: -len(tail)]
            break
    return var_name


# Variables whose presence signals multi-instance support in a role.
_OFFSET_VAR = re.compile(r"ports?_offset")
# Sibling variables worth including in the generated scenario.
_RELEVANT_VAR = re.compile(r"(ports?_offset|instance_name|service_name)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pick_reference(role_name: str, scenarios: list[ScenarioInfo]) -> str | None:
    for s in scenarios:
        if role_name in s.roles_used:
            return s.name
    for s in scenarios:
        if s.name == "default":
            return s.name
    return None


# ---------------------------------------------------------------------------
# Main analysis function
# ---------------------------------------------------------------------------

def analyze_gaps(ctx: CollectionContext) -> list[GapItem]:
    """Compare roles against existing scenarios → list of GapItems."""
    gaps: list[GapItem] = []

    all_roles_used     = {role for s in ctx.scenarios for role in s.roles_used}
    all_vars_set       = {var  for s in ctx.scenarios for var  in s.vars_set}
    all_scenario_names = [s.name for s in ctx.scenarios]
    role_names         = {r.name for r in ctx.roles}

    # Prefix derived from galaxy.yml collection name, used for standalone names.
    # e.g. collection_name="wildfly" → wildfly_firewalld → firewalld_standalone
    _collection_prefix = re.compile(r"^" + re.escape(ctx.collection_name) + r"_")

    for role in ctx.roles:

        # ── Gap type 1: role entirely uncovered ──────────────────────────
        if role.name not in all_roles_used:
            scenario_name = _collection_prefix.sub("", role.name) + "_standalone"
            required_vars = [v.name for v in role.variables if v.required]

            gaps.append(GapItem(
                role=role.name,
                type=GapType.ROLE_SCENARIO_ABSENT,
                reason=(
                    f"Role '{role.name}' is not exercised by any existing "
                    "molecule scenario."
                ),
                suggested_scenario_name=scenario_name,
                relevant_vars=required_vars,
                reference_scenario=_pick_reference(role.name, ctx.scenarios),
            ))
            # All sub-gap checks are implied by "uncovered" — skip them
            continue

        # ── Gap type 2: boolean feature flags never toggled ──────────────
        ref = _pick_reference(role.name, ctx.scenarios)
        for var in role.variables:
            if var.type != "bool":
                continue

            suffix = _auto_suffix(var.name, role_names)
            already_set  = var.name in all_vars_set
            name_covered = any(suffix in s for s in all_scenario_names)

            if not already_set and not name_covered:
                description = " ".join(var.description.split())
                gaps.append(GapItem(
                    role=role.name,
                    type=GapType.FLAG_NEVER_TOGGLED,
                    reason=(
                        f"Variable '{var.name}' ({description}) "
                        "is declared in argument_specs but never set to a "
                        "non-default value in any scenario."
                    ),
                    suggested_scenario_name=suffix,
                    relevant_vars=[var.name],
                    reference_scenario=ref,
                ))

    # ── Gap type 3: multi-instance / port offset never exercised ─────────
    # Discovered automatically: any role with a *port_offset* / *ports_offset*
    # variable in its argument_specs.yml is a multi-instance candidate.
    has_multi_scenario = any(
        "colocated" in s.name or "multi" in s.name for s in ctx.scenarios
    )
    for role in ctx.roles:
        offset_vars = [v for v in role.variables if _OFFSET_VAR.search(v.name)]
        if not offset_vars:
            continue
        covered = (
            any(v.name in all_vars_set for v in offset_vars) or has_multi_scenario
        )
        if covered:
            continue
        relevant_vars = [
            v.name for v in role.variables if _RELEVANT_VAR.search(v.name)
        ]
        ref = next(
            (s.name for s in ctx.scenarios if "colocated" in s.name), None
        )
        gaps.append(GapItem(
            role=role.name,
            type=GapType.MULTI_INSTANCE_MISSING,
            reason=(
                f"No scenario exercises multi-instance deployment "
                f"({offset_vars[0].name})."
            ),
            suggested_scenario_name="multi_instance",
            relevant_vars=relevant_vars,
            reference_scenario=ref,
        ))

    # Deduplicate by suggested_scenario_name
    seen: dict[str, GapItem] = {}
    for gap in gaps:
        if gap.suggested_scenario_name not in seen:
            seen[gap.suggested_scenario_name] = gap

    return list(seen.values())
