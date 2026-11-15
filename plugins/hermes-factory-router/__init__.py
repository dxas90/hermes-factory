"""
hermes-factory-router — delegation provider plugin for the Hermes Software Factory.

Adds a `stage` parameter to delegate_task so the factory orchestrator picks the
right model per pipeline stage (intake, spec, build, review-code, review-security,
review-ux, review-ui, approve) without hardcoding models in skill prompts.

Each stage maps to an entry under:
  delegation.backends.factory-router.stages.<stage-name>

which mirrors the `auxiliary:` pattern:
  stages:
    intake:
      provider: custom:hyperspaceai
      model: anthropic--claude-4.5-haiku
      base_url: ''
      api_key: ''
      timeout: 120
    spec:
      provider: custom:hyperspaceai
      model: anthropic--claude-4.6-opus
      ...

If a stage block is absent or has empty provider/model, the task falls through
to the default delegation route (inherits delegation.provider / delegation.model).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from agent.delegation_provider import DelegationProvider, DelegationRouteError  # type: ignore


# ── Default stage definitions ────────────────────────────────────────────────
# These are the out-of-box defaults matching the factory pipeline.
# Override any entry via delegation.backends.factory-router.stages.<name> in config.yaml.

_DEFAULT_STAGE_DESCRIPTIONS = {
    "intake":          "Reads a GitHub issue, decides ACCEPT/NEEDS-INFO/REJECT, rates risk and surfaces.",
    "spec":            "Turns the accepted scope into a spec.md with testable acceptance criteria.",
    "build":           "Implements the spec on a branch, runs checks, commits, opens a draft PR.",
    "review-code":     "Reviews the diff for correctness, simplicity and repo conventions.",
    "review-security": "Reviews the diff for auth, permissions, user input and secret handling.",
    "review-ux":       "Reviews UI/UX changes for user experience and accessibility.",
    "review-ui":       "Reviews visual layout, component usage and styling.",
    "approve":         "Final gate: APPROVE (merge) or ESCALATE (needs human).",
}

_ALL_STAGES = list(_DEFAULT_STAGE_DESCRIPTIONS.keys())


class FactoryRouter(DelegationProvider):

    @property
    def name(self) -> str:
        return "factory-router"

    # ── Schema exposed to the model ──────────────────────────────────────────

    def task_parameters(self, config: dict) -> dict:
        stages = self._stage_names(config)
        descriptions = self._stage_descriptions(config)
        desc_str = "; ".join(f"{s}: {descriptions.get(s, '')}" for s in stages)
        return {
            "stage": {
                "type": "string",
                "enum": stages,
                "description": (
                    "Factory pipeline stage for this subagent. "
                    f"Available stages — {desc_str}"
                ),
            }
        }

    def description(self, config: dict) -> str:
        return (
            "Set `stage` per task to route each factory subagent to the right model. "
            "Stages: " + ", ".join(self._stage_names(config)) + ". "
            "Each stage's provider/model is configured under "
            "delegation.backends.factory-router.stages.<stage> in config.yaml."
        )

    # ── Routing ──────────────────────────────────────────────────────────────

    def route_task(self, task: dict, config: dict) -> dict:
        stage = task.get("stage")
        if not stage:
            # No stage supplied — fall through to default delegation route.
            return dict(config)

        if stage not in self._stage_names(config):
            raise DelegationRouteError(
                f"Unknown factory stage {stage!r}. "
                f"Valid stages: {', '.join(self._stage_names(config))}."
            )

        stage_cfg = self._stage_config(config, stage)
        route: dict[str, Any] = {"label": stage}

        provider = stage_cfg.get("provider", "").strip()
        model    = stage_cfg.get("model",    "").strip()
        base_url = stage_cfg.get("base_url", "").strip()
        api_key  = stage_cfg.get("api_key",  "").strip()
        timeout  = stage_cfg.get("timeout")

        if provider:
            route["provider"] = provider
        if model:
            route["model"] = model
        if base_url:
            route["base_url"] = base_url
        if api_key:
            route["api_key"] = api_key

        # timeout is not a core route key — store it as a hint for the orchestrator
        # (the orchestrator skill reads it from config directly; we keep it in label
        # metadata for visibility in /delegation output).
        if timeout is not None:
            route["label"] = f"{stage} (timeout={timeout}s)"

        if not provider and not model:
            # Stage entry exists but both provider and model are empty —
            # inherit the default delegation route just like "no stage" would.
            return dict(config)

        return route

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _settings(self, config: dict) -> dict:
        """delegation.backends.factory-router block (may be empty)."""
        return self.settings(config) or {}

    def _stage_names(self, config: dict) -> list[str]:
        configured = self._settings(config).get("stages", {})
        # Always expose the full default set; add any extra configured stages.
        names = list(_ALL_STAGES)
        for extra in configured:
            if extra not in names:
                names.append(extra)
        return names

    def _stage_descriptions(self, config: dict) -> dict[str, str]:
        descs = dict(_DEFAULT_STAGE_DESCRIPTIONS)
        for stage, cfg in self._settings(config).get("stages", {}).items():
            if isinstance(cfg, dict) and cfg.get("description"):
                descs[stage] = cfg["description"]
        return descs

    def _stage_config(self, config: dict, stage: str) -> dict:
        return self._settings(config).get("stages", {}).get(stage, {})

    def is_available(self) -> bool:  # no network calls
        return True

    @property
    def display_name(self) -> str:
        return "Factory Router"

    @property
    def summary(self) -> str:
        return "Routes factory pipeline stages (intake/spec/build/review/approve) to configured models."


def register(ctx) -> None:  # type: ignore
    ctx.register_delegation_provider(FactoryRouter())
