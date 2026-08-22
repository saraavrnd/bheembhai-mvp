"""Unit tests — ADR-013 §5 env bundle composition from the init context.

No DB: an InitContext is assembled by hand; the bundle must be a pure function of
it. Credentials appear exactly where the vendor-key rule says, and nowhere else.
"""

import json
import uuid
from types import SimpleNamespace

from bheembhai.resolver import ResolvedIntegration

from engine_service.contexts import build_env_bundle, build_step_context
from engine_service.run_init import GitTarget, InitContext
from engine_service.workflow import PolicySpec, WorkflowSpec

WF_YAML = """
workflow: wf
start: story-design
steps:
  - id: story-design
    skill: story-design
    model: high
    "on":
      completed: DONE
"""
POLICY_YAML = "policy: fast\n"


def ri(iid, rtype, label, config, credential="sec-ret", ref="ref-1"):
    return ResolvedIntegration(integration_id=iid, type=rtype, label=label,
                               config=config, credential=credential, credential_ref=ref)


def make_ctx(*, vendor_type="claude", vendor_config=None, jira_config=None,
             git_config=None, env_vars=None, run_kind="workflow"):
    run = SimpleNamespace(id=uuid.UUID("12345678-1234-1234-1234-123456789abc"),
                          story_id="LNPRTL-101", run_kind=run_kind)
    wf = WorkflowSpec.load_yaml(WF_YAML)
    pol = PolicySpec.load_yaml(POLICY_YAML)
    github = ri("gh-1", "github", "GitHub", git_config or {
        "url": "https://github.com", "repository": "acme/demo",
        "base_branch": "main"}, "ghp_abcd", "github-token")
    vendor = ri("v-1", vendor_type, vendor_type.title(),
                vendor_config or {"model_high": "model-A", "model_medium": "model-B",
                                  "model_low": "model-C"},
                "sk-secret", "vendor-token")
    jira = ri("j-1", "jira", "Jira", jira_config) if jira_config is not None else None
    return InitContext(
        run=run, workflow_spec=wf, policy_spec=pol,
        github=github, ai_vendor=vendor, jira=jira,
        git_target=GitTarget("https://api.github.com", "https://github.com/acme/demo.git",
                             "acme/demo"),
        source_branch="main", run_branch="feat/lnprtl-101/140820260930-1234",
        model_map={"story-design": "model-A"}, skill_bundle={},
        env_vars=env_vars or {})


def bundle(**ctx_kwargs):
    ctx = make_ctx(**ctx_kwargs)
    context = build_step_context(str(ctx.run.id), "story-design", "story-design",
                                 "LNPRTL-101", ctx.workflow_spec, ctx.policy_spec)
    return build_env_bundle(ctx, step_id="story-design", attempt_no=1,
                            skill="story-design", model="model-A", context=context)


def test_engine_group():
    env = bundle()
    assert env["RUN_ID"] == "12345678-1234-1234-1234-123456789abc"
    assert env["STEP_ID"] == "story-design"
    assert env["ATTEMPT_NO"] == "1"
    assert env["SKILL"] == "story-design"
    assert env["RESULT_DIR"] == "/out"
    assert env["STORY_ID"] == "LNPRTL-101"
    # Phase 1 dropped the /ctx bind mount — the runner writes BB_CONTEXT to
    # CONTEXT_FILE under $HOME inside the container.
    assert env["CONTEXT_FILE"] == "/home/node/context.json"
    assert json.loads(env["BB_CONTEXT"])["run_id"] == "12345678-1234-1234-1234-123456789abc"


def test_no_skills_dir_env():
    # Skills arrive via BB_SKILL_URL at launch (state machine), never as a
    # mounted library path — the env bundle must not mention a skills dir.
    env = bundle()
    assert "BB_SKILLS_DIR" not in env


def test_no_put_urls_in_bundle():
    # The four PUT URLs (ADR-014) are launch-time presigns added in
    # state_machine — same pattern as BB_SKILL_URL. The env bundle itself
    # must never carry them: presigns expire and are per-attempt.
    env = bundle()
    assert not any("PUT_URL" in k for k in env)


def test_git_group():
    env = bundle()
    assert env["BB_GIT_MODE"] == "1"
    assert env["GIT_REMOTE_URL"] == "https://github.com/acme/demo.git"
    assert env["GIT_SOURCE_BRANCH"] == "main"
    assert env["RUN_BRANCH"] == "feat/lnprtl-101/140820260930-1234"
    assert env["GH_TOKEN"] == "ghp_abcd"


def test_model_group():
    env = bundle()
    assert env["BB_MODEL"] == "model-A"
    assert env["BB_ALLOWED_MODELS"] == "model-A,model-B,model-C"


def test_bb_mode_defaults_to_workflow_and_adhoc_switches():
    """ADR-016: the runner keys its prompt contract off BB_MODE — workflow
    steps keep the skill-vocabulary prompt, ad-hoc runs use the user query."""
    assert bundle()["BB_MODE"] == "workflow"
    assert bundle(run_kind="adhoc")["BB_MODE"] == "adhoc"


def test_claude_vendor_uses_anthropic_api_key():
    env = bundle(vendor_type="claude")
    assert env["ANTHROPIC_API_KEY"] == "sk-secret"
    assert "ANTHROPIC_AUTH_TOKEN" not in env


def test_non_claude_vendor_uses_auth_token_and_base_url():
    env = bundle(vendor_type="deepseek",
                 vendor_config={"model_high": "deepseek-v4-pro",
                                "model_medium": "deepseek-v4-flash",
                                "model_low": "deepseek-v4-flash",
                                "base_url": "https://api.deepseek.com"})
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-secret"
    assert env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com"
    assert "ANTHROPIC_API_KEY" not in env


def test_claude_base_url_only_when_configured():
    env = bundle(vendor_type="claude")
    assert "ANTHROPIC_BASE_URL" not in env
    env = bundle(vendor_type="claude",
                 vendor_config={"model_high": "m", "model_medium": "m", "model_low": "m",
                                "base_url": "https://proxy.example"})
    assert env["ANTHROPIC_BASE_URL"] == "https://proxy.example"


def test_jira_group_when_configured():
    env = bundle(jira_config={"url": "https://team.atlassian.net", "username": "sam@ex.com"})
    assert env["JIRA_URL"] == "https://team.atlassian.net"
    assert env["JIRA_USERNAME"] == "sam@ex.com"
    assert env["JIRA_EMAIL"] == "sam@ex.com"     # run_skill.sh prefers JIRA_EMAIL
    assert env["JIRA_API_TOKEN"] == "sec-ret"


def test_jira_absent_is_clean():
    env = bundle(jira_config=None)
    for key in ("JIRA_URL", "JIRA_USERNAME", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        assert key not in env, f"{key} leaked with no Jira integration"


def test_missing_model_maps_to_empty_string():
    ctx = make_ctx()
    context = build_step_context(str(ctx.run.id), "story-design", "story-design",
                                 "LNPRTL-101", ctx.workflow_spec, ctx.policy_spec)
    env = build_env_bundle(ctx, step_id="story-design", attempt_no=1,
                           skill="story-design", model=None, context=context)
    assert env["BB_MODEL"] == ""


# ── User-configured environment variables (platform + project, init-resolved) ──

def test_user_env_var_exported():
    env = bundle(env_vars={"TOOL_API_KEY": "user-secret-value",
                           "MY_PLAIN": "hello"})
    assert env["TOOL_API_KEY"] == "user-secret-value"
    assert env["MY_PLAIN"] == "hello"


def test_user_env_var_cannot_shadow_engine_keys():
    # Defense in depth (save-time validation is the primary gate): even if a
    # reserved name somehow reaches the bundle, engine-owned keys win.
    env = bundle(env_vars={"GH_TOKEN": "attacker-token",
                           "RUN_ID": "forged",
                           "BB_CONTEXT": "forged-context",
                           "BB_MAX_STEP_VISITS": "999"})
    assert env["GH_TOKEN"] == "ghp_abcd"
    assert env["RUN_ID"] == "12345678-1234-1234-1234-123456789abc"
    assert json.loads(env["BB_CONTEXT"])["run_id"] == "12345678-1234-1234-1234-123456789abc"
    # BB_MAX_* are NOT reserved — they flow through to the container and the
    # engine reads them separately as guardrail knobs.
    assert env["BB_MAX_STEP_VISITS"] == "999"


def test_tunables_pass_through():
    env = bundle(env_vars={"BB_MAX_STEP_VISITS": "1", "BB_MAX_ATTEMPTS": "5"})
    assert env["BB_MAX_STEP_VISITS"] == "1"
    assert env["BB_MAX_ATTEMPTS"] == "5"


def test_empty_env_vars_leaves_bundle_unchanged():
    # No configured variables → identical bundle to before the feature.
    env = bundle(env_vars={})
    assert "TOOL_API_KEY" not in env
    assert env["GH_TOKEN"] == "ghp_abcd"
