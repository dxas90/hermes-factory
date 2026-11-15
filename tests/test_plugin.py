"""
Tests for plugins/hermes-factory-router/__init__.py

Coverage:
  FactoryRouter.name / display_name / summary / is_available
  task_parameters     : returns stage enum with all 8 defaults + any extra configured
  description         : lists all stage names
  route_task          : no stage -> fall through
                        valid stage with provider+model -> route dict
                        valid stage with empty provider+model -> fall through
                        unknown stage -> DelegationRouteError
                        timeout present -> label modified
                        base_url / api_key forwarded when set
  _stage_names        : default stages + extra from config
  _stage_descriptions : default + override from config
  _stage_config       : present, absent
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── Stub the Hermes SDK import so the plugin loads without the runtime ─────────
_agent_mod = types.ModuleType("agent")
_delmod = types.ModuleType("agent.delegation_provider")

class _DelegationProvider:
    def settings(self, config: dict) -> dict:
        return config.get("delegation", {}).get("backends", {}).get(
            self.name, {})

class _DelegationRouteError(Exception):
    pass

_delmod.DelegationProvider = _DelegationProvider
_delmod.DelegationRouteError = _DelegationRouteError
_agent_mod.delegation_provider = _delmod
sys.modules.setdefault("agent", _agent_mod)
sys.modules.setdefault("agent.delegation_provider", _delmod)

_PLUGIN_PATH = (
    Path(__file__).resolve().parents[1]
    / "plugins/hermes-factory-router/__init__.py"
)
_spec = importlib.util.spec_from_file_location("factory_router", _PLUGIN_PATH)
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)

FactoryRouter = P.FactoryRouter
DelegationRouteError = _DelegationRouteError

# ── Config builder ────────────────────────────────────────────────────────────

def _cfg(stages=None, extra_stages=None):
    """Build a minimal config dict that the plugin reads via self.settings()."""
    s = {}
    if stages:
        s.update(stages)
    if extra_stages:
        s.update(extra_stages)
    return {
        "delegation": {
            "backends": {
                "factory-router": {"stages": s} if s else {}
            }
        }
    }


def _stage(provider="HyperspaceAI", model="haiku", base_url="",
           api_key="", timeout=None, description=None):
    d = {"provider": provider, "model": model,
         "base_url": base_url, "api_key": api_key}
    if timeout is not None:
        d["timeout"] = timeout
    if description:
        d["description"] = description
    return d


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Identity and availability
# ═══════════════════════════════════════════════════════════════════════════════

class TestIdentity(unittest.TestCase):

    def setUp(self):
        self.router = FactoryRouter()

    def test_name(self):
        self.assertEqual(self.router.name, "factory-router")

    def test_display_name(self):
        self.assertIn("Factory", self.router.display_name)

    def test_summary(self):
        self.assertIn("stage", self.router.summary.lower())

    def test_is_available(self):
        self.assertTrue(self.router.is_available())


# ═══════════════════════════════════════════════════════════════════════════════
# 2. task_parameters
# ═══════════════════════════════════════════════════════════════════════════════

class TestTaskParameters(unittest.TestCase):

    def setUp(self):
        self.router = FactoryRouter()

    def test_returns_stage_parameter(self):
        params = self.router.task_parameters(_cfg())
        self.assertIn("stage", params)

    def test_stage_is_enum(self):
        params = self.router.task_parameters(_cfg())
        self.assertEqual(params["stage"]["type"], "string")
        self.assertIn("enum", params["stage"])

    def test_all_default_stages_present(self):
        params = self.router.task_parameters(_cfg())
        for stage in P._ALL_STAGES:
            self.assertIn(stage, params["stage"]["enum"],
                          f"Default stage {stage!r} missing from enum")

    def test_extra_configured_stage_added(self):
        cfg = _cfg(extra_stages={"my-custom-stage": _stage()})
        params = self.router.task_parameters(cfg)
        self.assertIn("my-custom-stage", params["stage"]["enum"])

    def test_description_mentions_all_stages(self):
        params = self.router.task_parameters(_cfg())
        desc = params["stage"]["description"]
        for stage in P._ALL_STAGES:
            self.assertIn(stage, desc)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. description()
# ═══════════════════════════════════════════════════════════════════════════════

class TestDescription(unittest.TestCase):

    def test_lists_stages(self):
        router = FactoryRouter()
        desc = router.description(_cfg())
        for stage in P._ALL_STAGES:
            self.assertIn(stage, desc)


# ═══════════════════════════════════════════════════════════════════════════════
# 4. route_task: core routing
# ═══════════════════════════════════════════════════════════════════════════════

class TestRouteTask(unittest.TestCase):

    def setUp(self):
        self.router = FactoryRouter()
        self.full_cfg = _cfg(stages={
            "intake":          _stage("ProvA", "haiku", timeout=120),
            "spec":            _stage("ProvB", "opus"),
            "build":           _stage("ProvB", "opus"),
            "review-code":     _stage("ProvA", "haiku"),
            "review-security": _stage("ProvB", "opus"),
            "review-ux":       _stage("ProvA", "sonnet"),
            "review-ui":       _stage("ProvA", "haiku"),
            "approve":         _stage("ProvB", "opus"),
        })

    def test_no_stage_falls_through(self):
        result = self.router.route_task({}, self.full_cfg)
        self.assertNotIn("provider", result)
        self.assertNotIn("model", result)

    def test_valid_stage_sets_provider_and_model(self):
        result = self.router.route_task({"stage": "intake"}, self.full_cfg)
        self.assertEqual(result["provider"], "ProvA")
        self.assertEqual(result["model"], "haiku")

    def test_valid_stage_sets_label(self):
        result = self.router.route_task({"stage": "spec"}, self.full_cfg)
        self.assertIn("label", result)

    def test_timeout_appears_in_label(self):
        result = self.router.route_task({"stage": "intake"}, self.full_cfg)
        self.assertIn("120", result["label"])

    def test_unknown_stage_raises(self):
        with self.assertRaises(DelegationRouteError) as ctx:
            self.router.route_task({"stage": "nonexistent"}, self.full_cfg)
        self.assertIn("nonexistent", str(ctx.exception))

    def test_empty_provider_and_model_falls_through(self):
        cfg = _cfg(stages={"intake": {"provider": "", "model": ""}})
        result = self.router.route_task({"stage": "intake"}, cfg)
        # Must fall through — not set provider/model
        self.assertNotIn("provider", result)
        self.assertNotIn("model", result)

    def test_base_url_forwarded(self):
        cfg = _cfg(stages={"intake": _stage("P", "m", base_url="https://custom.api")})
        result = self.router.route_task({"stage": "intake"}, cfg)
        self.assertEqual(result["base_url"], "https://custom.api")

    def test_empty_base_url_not_forwarded(self):
        cfg = _cfg(stages={"intake": _stage("P", "m", base_url="")})
        result = self.router.route_task({"stage": "intake"}, cfg)
        self.assertNotIn("base_url", result)

    def test_api_key_forwarded(self):
        cfg = _cfg(stages={"intake": _stage("P", "m", api_key="sk-secret")})
        result = self.router.route_task({"stage": "intake"}, cfg)
        self.assertEqual(result["api_key"], "sk-secret")

    def test_empty_api_key_not_forwarded(self):
        cfg = _cfg(stages={"intake": _stage("P", "m", api_key="")})
        result = self.router.route_task({"stage": "intake"}, cfg)
        self.assertNotIn("api_key", result)

    def test_all_eight_stages_routable(self):
        for stage in P._ALL_STAGES:
            with self.subTest(stage=stage):
                result = self.router.route_task({"stage": stage}, self.full_cfg)
                self.assertIn("provider", result)
                self.assertIn("model", result)

    def test_provider_only_empty_model_falls_through(self):
        """If only provider is set but model is empty, fall through."""
        cfg = _cfg(stages={"intake": {"provider": "SomeProvider", "model": ""}})
        result = self.router.route_task({"stage": "intake"}, cfg)
        # Both must be non-empty to route; one empty -> fall through
        self.assertNotIn("model", result)

    def test_model_only_empty_provider_falls_through(self):
        """If only model is set but provider is empty, fall through."""
        cfg = _cfg(stages={"intake": {"provider": "", "model": "some-model"}})
        result = self.router.route_task({"stage": "intake"}, cfg)
        self.assertNotIn("provider", result)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. _stage_names and _stage_descriptions
# ═══════════════════════════════════════════════════════════════════════════════

class TestStageHelpers(unittest.TestCase):

    def setUp(self):
        self.router = FactoryRouter()

    def test_stage_names_includes_all_defaults(self):
        names = self.router._stage_names(_cfg())
        for s in P._ALL_STAGES:
            self.assertIn(s, names)

    def test_stage_names_includes_extra(self):
        cfg = _cfg(extra_stages={"custom-review": _stage()})
        names = self.router._stage_names(cfg)
        self.assertIn("custom-review", names)

    def test_stage_names_no_duplicates(self):
        # Configuring an existing stage name must not duplicate it
        cfg = _cfg(stages={"intake": _stage()})
        names = self.router._stage_names(cfg)
        self.assertEqual(names.count("intake"), 1)

    def test_stage_descriptions_default(self):
        descs = self.router._stage_descriptions(_cfg())
        for s in P._ALL_STAGES:
            self.assertIn(s, descs)
            self.assertIsNotNone(descs[s])

    def test_stage_descriptions_override(self):
        cfg = _cfg(stages={"intake": _stage(description="Custom intake desc")})
        descs = self.router._stage_descriptions(cfg)
        self.assertEqual(descs["intake"], "Custom intake desc")

    def test_stage_descriptions_no_override_keeps_default(self):
        cfg = _cfg(stages={"spec": _stage()})  # no description key
        descs = self.router._stage_descriptions(cfg)
        # spec has no description override -> keeps default
        self.assertEqual(descs["spec"], P._DEFAULT_STAGE_DESCRIPTIONS["spec"])

    def test_stage_config_absent_returns_empty(self):
        result = self.router._stage_config(_cfg(), "nonexistent")
        self.assertEqual(result, {})

    def test_stage_config_present_returns_dict(self):
        cfg = _cfg(stages={"intake": _stage("P", "m")})
        result = self.router._stage_config(cfg, "intake")
        self.assertEqual(result["provider"], "P")
        self.assertEqual(result["model"], "m")


# ═══════════════════════════════════════════════════════════════════════════════
# 6. register()
# ═══════════════════════════════════════════════════════════════════════════════

class TestRegister(unittest.TestCase):

    def test_register_calls_register_delegation_provider(self):
        ctx = MagicMock()
        P.register(ctx)
        ctx.register_delegation_provider.assert_called_once()
        provider = ctx.register_delegation_provider.call_args[0][0]
        self.assertIsInstance(provider, FactoryRouter)


if __name__ == "__main__":
    unittest.main()
