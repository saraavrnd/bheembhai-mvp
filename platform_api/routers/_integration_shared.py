"""Shared integration helpers — used by both the admin router and the
project-scoped (PM) router.

Keeping the type registry, SecureStorage accessor, status computation and
response builder in one module avoids importing the 1800-line admin router
from the project-scoped integrations router.
"""

from datetime import datetime as _dt, timedelta as _td, timezone as _tz
import logging

import httpx
from fastapi import HTTPException, Request

from bheembhai.models.project import ProjectIntegration

from platform_api.schemas.admin import IntegrationAdminResponse, TestConnectionResult

logger = logging.getLogger(__name__)


# AI vendor integration types — used for tier-config validation and run
# submission checks (a run selects exactly one AI vendor integration).
AI_VENDOR_TYPES: set[str] = {"openai", "claude", "deepseek", "kimi"}

# Tier keys every AI vendor config must map (high/medium/low → model id).
MODEL_TIER_KEYS: tuple[str, ...] = ("model_high", "model_medium", "model_low")


# Integration type registry — shared with the UI template.
# Each entry defines the label, icon, category, and ordered form fields.
INTEGRATION_TYPE_REGISTRY: dict[str, dict] = {
    "jira": {
        "key": "jira", "label": "Jira", "category": "TOOLS", "icon": "JR",
        "description": "Jira issue tracking integration",
        "fields": [
            {"name": "url", "label": "Jira URL", "field_type": "text", "required": True, "placeholder": "https://your-domain.atlassian.net"},
            {"name": "username", "label": "Username (email)", "field_type": "text", "required": True, "placeholder": "you@example.com"},
            {"name": "api_token", "label": "API token", "field_type": "secret", "required": True, "placeholder": ""},
            {"name": "project_key", "label": "Project key", "field_type": "text", "required": False, "placeholder": "PROJ"},
            {"name": "default_issue_type", "label": "Default issue type", "field_type": "text", "required": False, "placeholder": "Task"},
        ],
    },
    "github": {
        "key": "github", "label": "GitHub", "category": "TOOLS", "icon": "GH",
        "description": "GitHub source control and PR integration",
        "fields": [
            {"name": "url", "label": "GitHub URL", "field_type": "text", "required": False, "placeholder": "https://github.com"},
            {"name": "username", "label": "Username", "field_type": "text", "required": False, "placeholder": "your-username"},
            {"name": "access_token", "label": "Access token", "field_type": "secret", "required": True, "placeholder": ""},
            {"name": "repository", "label": "Repository", "field_type": "text", "required": False, "placeholder": "owner/repo"},
            {"name": "base_branch", "label": "Base branch", "field_type": "text", "required": False, "placeholder": "main"},
        ],
    },
    "openai": {
        "key": "openai", "label": "OpenAI", "category": "AI VENDORS", "icon": "OA",
        "description": "Model tiers let each stage pick the cheapest model",
        "fields": [
            {"name": "base_url", "label": "Base URL", "field_type": "text", "required": True, "placeholder": "https://api.openai.com/v1"},
            {"name": "api_token", "label": "API token", "field_type": "secret", "required": True, "placeholder": ""},
            {"name": "organisation", "label": "Organisation", "field_type": "text", "required": False, "placeholder": "org-..."},
            {"name": "model_high", "label": "High-end model", "field_type": "select", "required": True,
             "options": [{"value": "gpt-5-pro", "label": "gpt-5-pro"}, {"value": "gpt-5", "label": "gpt-5"}]},
            {"name": "model_medium", "label": "Medium model", "field_type": "select", "required": True,
             "options": [{"value": "gpt-5", "label": "gpt-5"}, {"value": "gpt-5-mini", "label": "gpt-5-mini"}]},
            {"name": "model_low", "label": "Low-tier model", "field_type": "select", "required": True,
             "options": [{"value": "gpt-5-mini", "label": "gpt-5-mini"}]},
            {"name": "timeout", "label": "Request timeout", "field_type": "text", "required": False, "placeholder": "120s"},
        ],
    },
    "claude": {
        "key": "claude", "label": "Claude", "category": "AI VENDORS", "icon": "CL",
        "description": "Anthropic Claude model tiers",
        "fields": [
            {"name": "base_url", "label": "Base URL", "field_type": "text", "required": True, "placeholder": "https://api.anthropic.com/v1"},
            {"name": "api_token", "label": "API token", "field_type": "secret", "required": True, "placeholder": ""},
            {"name": "organisation", "label": "Organisation", "field_type": "text", "required": False, "placeholder": ""},
            {"name": "model_high", "label": "High-end model", "field_type": "select", "required": True,
             "options": [{"value": "claude-opus-5", "label": "claude-opus-5"}, {"value": "claude-sonnet-5", "label": "claude-sonnet-5"}]},
            {"name": "model_medium", "label": "Medium model", "field_type": "select", "required": True,
             "options": [{"value": "claude-sonnet-5", "label": "claude-sonnet-5"}, {"value": "claude-haiku-4-5", "label": "claude-haiku-4-5"}]},
            {"name": "model_low", "label": "Low-tier model", "field_type": "select", "required": True,
             "options": [{"value": "claude-haiku-4-5", "label": "claude-haiku-4-5"}]},
            {"name": "timeout", "label": "Request timeout", "field_type": "text", "required": False, "placeholder": "120s"},
        ],
    },
    "kimi": {
        "key": "kimi", "label": "Kimi", "category": "AI VENDORS", "icon": "KM",
        "description": "Moonshot Kimi model tiers",
        "fields": [
            {"name": "base_url", "label": "Base URL", "field_type": "text", "required": True, "placeholder": "https://api.moonshot.cn/v1"},
            {"name": "api_token", "label": "API token", "field_type": "secret", "required": True, "placeholder": ""},
            {"name": "organisation", "label": "Organisation", "field_type": "text", "required": False, "placeholder": ""},
            {"name": "model_high", "label": "High-end model", "field_type": "select", "required": True,
             "options": [{"value": "kimi-k2", "label": "kimi-k2"}]},
            {"name": "model_medium", "label": "Medium model", "field_type": "select", "required": True,
             "options": [{"value": "kimi-k2", "label": "kimi-k2"}]},
            {"name": "model_low", "label": "Low-tier model", "field_type": "select", "required": True,
             "options": [{"value": "kimi-k2", "label": "kimi-k2"}]},
            {"name": "timeout", "label": "Request timeout", "field_type": "text", "required": False, "placeholder": "120s"},
        ],
    },
    "deepseek": {
        "key": "deepseek", "label": "DeepSeek", "category": "AI VENDORS", "icon": "DS",
        "description": "DeepSeek model tiers",
        "fields": [
            {"name": "base_url", "label": "Base URL", "field_type": "text", "required": True, "placeholder": "https://api.deepseek.com/v1"},
            {"name": "api_token", "label": "API token", "field_type": "secret", "required": True, "placeholder": ""},
            {"name": "organisation", "label": "Organisation", "field_type": "text", "required": False, "placeholder": ""},
            {"name": "model_high", "label": "High-end model", "field_type": "select", "required": True,
             "options": [{"value": "deepseek-v4-pro", "label": "deepseek-v4-pro"}, {"value": "deepseek-v4-flash", "label": "deepseek-v4-flash"}]},
            {"name": "model_medium", "label": "Medium model", "field_type": "select", "required": True,
             "options": [{"value": "deepseek-v4-pro", "label": "deepseek-v4-pro"}, {"value": "deepseek-v4-flash", "label": "deepseek-v4-flash"}]},
            {"name": "model_low", "label": "Low-tier model", "field_type": "select", "required": True,
             "options": [{"value": "deepseek-v4-flash", "label": "deepseek-v4-flash"}]},
            {"name": "timeout", "label": "Request timeout", "field_type": "text", "required": False, "placeholder": "120s"},
        ],
    },
}


def validate_ai_vendor_config(type_: str, config: dict) -> None:
    """Validate an AI-vendor integration's config before persisting.

    All three tier keys (model_high/model_medium/model_low) must be present
    and non-empty — the engine resolves workflow step tiers through them at
    run initialization. Non-AI-vendor types are not checked here.
    """
    if type_ not in AI_VENDOR_TYPES:
        return
    missing = [k for k in MODEL_TIER_KEYS if not str(config.get(k) or "").strip()]
    if missing:
        raise HTTPException(
            422,
            f"Model tier mapping incomplete — required: {', '.join(MODEL_TIER_KEYS)}; "
            f"missing: {', '.join(missing)}",
        )


def _secure_storage(request: Request):
    """Return the SecureStorage provider wired at startup."""
    ss = getattr(request.app.state, "secure_storage", None)
    if ss is None:
        raise HTTPException(500, "Secure storage backend is not configured")
    return ss


def _integration_status(integ: ProjectIntegration | None) -> str:
    """Compute status-dot value for an integration type slot.

    - ``connected``: integration exists and was verified recently (≤ 30 days)
    - ``warning``: integration exists but hasn't been verified recently (> 30 days)
    - ``unconfigured``: no integration of this type exists
    """
    if integ is None:
        return "unconfigured"
    if integ.verified_at is None:
        return "warning"
    age = _dt.now(_tz.utc) - integ.verified_at
    if age > _td(days=30):
        return "warning"
    return "connected"


def _integration_to_response(integ: ProjectIntegration) -> IntegrationAdminResponse:
    """Build an admin-facing integration response."""
    status = _integration_status(integ)
    return IntegrationAdminResponse(
        id=str(integ.id),
        project_id=str(integ.project_id),
        type=integ.type,
        label=integ.label,
        credential_ref=integ.credential_ref,
        config=integ.config or {},
        verified_at=integ.verified_at.isoformat() if integ.verified_at else None,
        created_at=integ.created_at.isoformat() if integ.created_at else "",
        status=status,
    )


async def _test_integration_connection(
    integration: ProjectIntegration,
    credential_value: str,
) -> TestConnectionResult:
    """Attempt a lightweight authenticated API call for an integration.

    Shared by the admin and project-manager routers — keeps the per-type
    probe logic (jira / github / AI vendor) in one place. Callers update
    ``verified_at`` themselves when ``ok`` is True.
    """
    config = integration.config or {}

    def _log_probe(probe: str, url: str, resp: httpx.Response) -> None:
        """Debug-log one probe request: url, status and a truncated body."""
        body = (resp.text or "")[:500].replace("\n", " ")
        logger.debug(
            "test_connection probe[%s] type=%s label=%s url=%s status=%s body=%r",
            probe, integration.type, integration.label, url, resp.status_code, body,
        )

    # Credential fingerprint only — never the credential value itself.
    logger.debug(
        "test_connection start: type=%s label=%s url=%s credential_last4=%s",
        integration.type, integration.label,
        config.get("base_url") or config.get("url") or "<none>",
        credential_value[-4:] if credential_value else "<empty>",
    )

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            if integration.type == "jira":
                url = config.get("url", "https://your-domain.atlassian.net").rstrip("/")
                username = config.get("username", "")
                if not username:
                    return TestConnectionResult(ok=False, message="Username (email) is required in the config.")
                resp = await client.get(
                    f"{url}/rest/api/2/myself",
                    auth=(username, credential_value),
                )
                _log_probe("jira/myself", f"{url}/rest/api/2/myself", resp)
                if resp.status_code in (200, 201):
                    data = resp.json()
                    return TestConnectionResult(
                        ok=True,
                        message=f"Connected as {data.get('displayName', 'unknown')} ({data.get('emailAddress', '')})",
                        details={"user": data.get("displayName"), "email": data.get("emailAddress")},
                    )
                return TestConnectionResult(
                    ok=False,
                    message=f"Jira returned HTTP {resp.status_code}",
                    details={"status": resp.status_code, "body": resp.text[:500]},
                )

            if integration.type == "github":
                url = config.get("url", "https://api.github.com").rstrip("/")
                if "api.github.com" not in url:
                    url = "https://api.github.com"
                resp = await client.get(
                    f"{url}/user",
                    headers={
                        "Authorization": f"Bearer {credential_value}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                )
                _log_probe("github/user", f"{url}/user", resp)
                if resp.status_code == 200:
                    data = resp.json()
                    return TestConnectionResult(
                        ok=True,
                        message=f"Authenticated as {data.get('login', 'unknown')}",
                        details={"login": data.get("login"), "name": data.get("name")},
                    )
                return TestConnectionResult(
                    ok=False,
                    message=f"GitHub returned HTTP {resp.status_code}",
                    details={"status": resp.status_code, "body": resp.text[:500]},
                )

            # AI Vendor — probe the endpoint style it speaks:
            # 1. OpenAI-style: GET {base}/models with Bearer auth
            # 2. Anthropic-style: POST {base}/v1/messages with x-api-key
            #    (claude, DeepSeek's /anthropic endpoint, …) — a 1-token ping
            #    proves both the credential and the tier model mapping
            # 3. Last resort: GET {base}
            base_url = config.get("base_url", "").rstrip("/")
            if not base_url:
                return TestConnectionResult(ok=False, message="Base URL is required in the config.")
            resp = await client.get(
                f"{base_url}/models",
                headers={"Authorization": f"Bearer {credential_value}"},
            )
            _log_probe("vendor/models", f"{base_url}/models", resp)
            if resp.status_code in (200, 201):
                data = resp.json()
                model_count = len(data.get("data", data if isinstance(data, list) else []))
                return TestConnectionResult(
                    ok=True,
                    message=f"Connected · {model_count} models resolved",
                    details={"model_count": model_count},
                )

            # Anthropic-style endpoints don't serve GET /models (404) — they
            # authenticate with x-api-key + anthropic-version.
            ping_model = str(config.get("model_medium") or config.get("model_low") or "")
            messages_url = f"{base_url}/v1/messages"
            resp2 = await client.post(
                messages_url,
                headers={
                    "x-api-key": credential_value,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": ping_model,
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "ping"}],
                },
            )
            _log_probe("vendor/messages", messages_url, resp2)
            if resp2.status_code in (200, 201):
                return TestConnectionResult(
                    ok=True,
                    message="Connected (Anthropic-style messages endpoint)",
                    details={"status": resp2.status_code, "model": ping_model},
                )

            # Some APIs return 404 on /models — try hitting the base URL
            resp3 = await client.get(
                base_url,
                headers={"Authorization": f"Bearer {credential_value}"},
            )
            _log_probe("vendor/base", base_url, resp3)
            if resp3.status_code in (200, 401, 403):
                return TestConnectionResult(
                    ok=True,
                    message="Connected (endpoint reached, auth accepted)",
                    details={"status": resp3.status_code},
                )
            # Report the most informative failure: the messages probe when it
            # answered (auth/model errors carry a body), else the /models probe.
            if resp2.status_code:
                return TestConnectionResult(
                    ok=False,
                    message=f"Vendor returned HTTP {resp2.status_code}",
                    details={"status": resp2.status_code, "body": resp2.text[:500]},
                )
            return TestConnectionResult(
                ok=False,
                message=f"Vendor returned HTTP {resp.status_code}",
                details={"status": resp.status_code, "body": resp.text[:500]},
            )
    except httpx.ConnectError as exc:
        logger.debug("test_connection ConnectError: %s", exc, exc_info=True)
        return TestConnectionResult(ok=False, message="Connection refused — check the URL.")
    except httpx.TimeoutException as exc:
        logger.debug("test_connection TimeoutException: %s", exc, exc_info=True)
        return TestConnectionResult(ok=False, message="Connection timed out after 15 seconds.")
    except Exception as exc:
        logger.debug("test_connection unexpected error: %s", exc, exc_info=True)
        return TestConnectionResult(ok=False, message=f"Unexpected error: {exc}")
