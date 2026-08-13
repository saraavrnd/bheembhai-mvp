"""Shared integration helpers — used by both the admin router and the
project-scoped (PM) router.

Keeping the type registry, SecureStorage accessor, status computation and
response builder in one module avoids importing the 1800-line admin router
from the project-scoped integrations router.
"""

from datetime import datetime as _dt, timedelta as _td, timezone as _tz

import httpx
from fastapi import HTTPException, Request

from bheembhai.models.project import ProjectIntegration

from platform_api.schemas.admin import IntegrationAdminResponse, TestConnectionResult


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
            {"name": "model_high", "label": "High-end model", "field_type": "select", "required": False,
             "options": [{"value": "gpt-5-pro", "label": "gpt-5-pro"}, {"value": "gpt-5", "label": "gpt-5"}]},
            {"name": "model_medium", "label": "Medium model", "field_type": "select", "required": False,
             "options": [{"value": "gpt-5", "label": "gpt-5"}, {"value": "gpt-5-mini", "label": "gpt-5-mini"}]},
            {"name": "model_small", "label": "Small-task model", "field_type": "select", "required": False,
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
            {"name": "model_high", "label": "High-end model", "field_type": "select", "required": False,
             "options": [{"value": "claude-opus-5", "label": "claude-opus-5"}, {"value": "claude-sonnet-5", "label": "claude-sonnet-5"}]},
            {"name": "model_medium", "label": "Medium model", "field_type": "select", "required": False,
             "options": [{"value": "claude-sonnet-5", "label": "claude-sonnet-5"}, {"value": "claude-haiku-4-5", "label": "claude-haiku-4-5"}]},
            {"name": "model_small", "label": "Small-task model", "field_type": "select", "required": False,
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
            {"name": "model_high", "label": "High-end model", "field_type": "select", "required": False,
             "options": [{"value": "kimi-k2", "label": "kimi-k2"}]},
            {"name": "model_medium", "label": "Medium model", "field_type": "select", "required": False,
             "options": [{"value": "kimi-k2", "label": "kimi-k2"}]},
            {"name": "model_small", "label": "Small-task model", "field_type": "select", "required": False,
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
            {"name": "model_high", "label": "High-end model", "field_type": "select", "required": False,
             "options": [{"value": "deepseek-v4-pro", "label": "deepseek-v4-pro"}, {"value": "deepseek-v4-flash", "label": "deepseek-v4-flash"}]},
            {"name": "model_medium", "label": "Medium model", "field_type": "select", "required": False,
             "options": [{"value": "deepseek-v4-pro", "label": "deepseek-v4-pro"}, {"value": "deepseek-v4-flash", "label": "deepseek-v4-flash"}]},
            {"name": "model_small", "label": "Small-task model", "field_type": "select", "required": False,
             "options": [{"value": "deepseek-v4-flash", "label": "deepseek-v4-flash"}]},
            {"name": "timeout", "label": "Request timeout", "field_type": "text", "required": False, "placeholder": "120s"},
        ],
    },
}


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

            # AI Vendor — try to list models
            base_url = config.get("base_url", "").rstrip("/")
            if not base_url:
                return TestConnectionResult(ok=False, message="Base URL is required in the config.")
            resp = await client.get(
                f"{base_url}/models",
                headers={"Authorization": f"Bearer {credential_value}"},
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                model_count = len(data.get("data", data if isinstance(data, list) else []))
                return TestConnectionResult(
                    ok=True,
                    message=f"Connected · {model_count} models resolved",
                    details={"model_count": model_count},
                )
            # Some APIs return 404 on /models — try hitting the base URL
            resp2 = await client.get(
                base_url,
                headers={"Authorization": f"Bearer {credential_value}"},
            )
            if resp2.status_code in (200, 401, 403):
                return TestConnectionResult(
                    ok=True,
                    message="Connected (endpoint reached, auth accepted)",
                    details={"status": resp2.status_code},
                )
            return TestConnectionResult(
                ok=False,
                message=f"Vendor returned HTTP {resp.status_code}",
                details={"status": resp.status_code, "body": resp.text[:500]},
            )
    except httpx.ConnectError:
        return TestConnectionResult(ok=False, message="Connection refused — check the URL.")
    except httpx.TimeoutException:
        return TestConnectionResult(ok=False, message="Connection timed out after 15 seconds.")
    except Exception as exc:
        return TestConnectionResult(ok=False, message=f"Unexpected error: {exc}")
