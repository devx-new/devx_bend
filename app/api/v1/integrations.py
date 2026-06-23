import secrets

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.config import settings
from app.core.rate_limit import get_redis
from app.database import get_db
from app.models.integration import Integration
from app.models.routing import RoutingRule
from app.models.user import User
from app.schemas.integration import (
    IntegrationConfigureRequest,
    IntegrationCreate,
    IntegrationResponse,
    ProviderCatalogItem,
)
from app.schemas.routing import RoutingRuleCreate, RoutingRuleUpdate, RoutingRuleResponse

router = APIRouter(prefix="/integrations", tags=["integrations"])

# ---------------------------------------------------------------------------
# Provider catalog — defines every integration the product supports.
# "status" and "meta" are merged from the DB at request time.
# ---------------------------------------------------------------------------
_CATALOG: dict[str, dict] = {
    "github": {
        "name": "GitHub",
        "description": "Two-way sync for issues, pull requests, and commit metadata.",
        "scopes": ["read", "write:issues"],
        "coming_soon": False,
    },
    "linear": {
        "name": "Linear",
        "description": "Streamline issue creation and track project progress directly from feedback.",
        "scopes": ["read", "write"],
        "coming_soon": False,
    },
    "slack": {
        "name": "Slack",
        "description": "Receive real-time notifications and capture feedback straight from channels.",
        "scopes": ["channels:read", "chat:write"],
        "coming_soon": False,
    },
    "discord": {
        "name": "Discord",
        "description": "Engage your community and collect bug reports directly from Discord servers.",
        "scopes": ["guilds", "messages"],
        "coming_soon": False,
    },
    "jira": {
        "name": "Jira Software",
        "description": "Automatically create Jira issues from incoming feedback.",
        "scopes": ["read:jira-work", "write:jira-work"],
        "coming_soon": False,
    },
    "clickup": {
        "name": "ClickUp",
        "description": "Create ClickUp tasks automatically from high-priority feedback.",
        "scopes": ["task:write", "team:read"],
        "coming_soon": False,
    },
}

# OAuth state TTL (seconds) — stored in Redis to prevent CSRF on callback
_OAUTH_STATE_TTL = 600


async def _get_integration(db: AsyncSession, tenant_id: str, provider: str) -> Integration | None:
    result = await db.execute(
        select(Integration).where(
            Integration.tenant_id == tenant_id,
            Integration.provider == provider,
        )
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Catalog + list
# ---------------------------------------------------------------------------

@router.get("/providers")
async def list_providers():
    """All supported integration providers with capability metadata."""
    return {"success": True, "data": list(_CATALOG.items())}


@router.get("")
async def list_integrations(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Full provider catalog merged with the tenant's connected integrations."""
    tenant_id = current_user.tenant_id
    result = await db.execute(select(Integration).where(Integration.tenant_id == tenant_id))
    connected = {row.provider: row for row in result.scalars().all()}

    items = []
    for provider, info in _CATALOG.items():
        row = connected.get(provider)
        items.append(
            ProviderCatalogItem(
                provider=provider,
                name=info["name"],
                description=info["description"],
                scopes=info["scopes"],
                coming_soon=info["coming_soon"],
                status="connected" if row else "not_connected",
                last_sync_at=row.last_sync_at if row else None,
                meta=row.config if row else None,
                integration_id=row.id if row else None,
            )
        )

    return {"success": True, "data": [i.model_dump() for i in items]}


# ---------------------------------------------------------------------------
# OAuth — step 1: get authorization URL
# ---------------------------------------------------------------------------

@router.get("/{provider}/oauth-url")
async def get_oauth_url(
    provider: str,
    current_user: User = Depends(get_current_user),
):
    """Return the OAuth authorization URL for the given provider."""
    if provider not in _CATALOG or _CATALOG[provider]["coming_soon"]:
        raise HTTPException(status_code=400, detail=f"Provider '{provider}' not available")

    state = secrets.token_urlsafe(32)
    try:
        r = await get_redis()
        await r.setex(f"oauth_state:{state}", _OAUTH_STATE_TTL, current_user.tenant_id)
    except Exception:
        pass  # Redis unavailable — state validation degraded but not blocking

    if provider == "github":
        if not settings.github_client_id:
            raise HTTPException(status_code=503, detail="GitHub OAuth not configured")
        redirect = settings.github_integration_redirect_uri or settings.github_redirect_uri
        url = (
            f"https://github.com/login/oauth/authorize"
            f"?client_id={settings.github_client_id}"
            f"&redirect_uri={redirect}"
            f"&scope=repo,read:org"
            f"&state={state}"
        )
    elif provider == "linear":
        if not settings.linear_client_id:
            raise HTTPException(status_code=503, detail="Linear OAuth not configured")
        url = (
            f"https://linear.app/oauth/authorize"
            f"?client_id={settings.linear_client_id}"
            f"&redirect_uri={settings.linear_redirect_uri}"
            f"&response_type=code"
            f"&scope=read,write"
            f"&state={state}"
        )
    elif provider == "slack":
        if not settings.slack_client_id:
            raise HTTPException(status_code=503, detail="Slack OAuth not configured")
        url = (
            f"https://slack.com/oauth/v2/authorize"
            f"?client_id={settings.slack_client_id}"
            f"&redirect_uri={settings.slack_redirect_uri}"
            f"&scope=channels:read,channels:history,groups:history,app_mentions:read,chat:write"
            f"&state={state}"
        )
    elif provider == "jira":
        if not settings.jira_client_id:
            raise HTTPException(status_code=503, detail="Jira OAuth not configured")
        url = (
            f"https://auth.atlassian.com/authorize"
            f"?audience=api.atlassian.com"
            f"&client_id={settings.jira_client_id}"
            f"&scope=read:jira-work%20write:jira-work%20offline_access"
            f"&redirect_uri={settings.jira_redirect_uri}"
            f"&state={state}"
            f"&response_type=code"
            f"&prompt=consent"
        )
    elif provider == "discord":
        if not settings.discord_client_id:
            raise HTTPException(status_code=503, detail="Discord OAuth not configured")
        url = (
            f"https://discord.com/api/oauth2/authorize"
            f"?client_id={settings.discord_client_id}"
            f"&redirect_uri={settings.discord_redirect_uri}"
            f"&response_type=code"
            f"&scope=bot%20applications.commands"
            f"&permissions=2048"
            f"&state={state}"
        )
    elif provider == "clickup":
        if not settings.clickup_client_id:
            raise HTTPException(status_code=503, detail="ClickUp OAuth not configured")
        url = (
            f"https://app.clickup.com/api"
            f"?client_id={settings.clickup_client_id}"
            f"&redirect_uri={settings.clickup_redirect_uri}"
            f"&state={state}"
        )
    else:
        raise HTTPException(status_code=400, detail=f"OAuth not implemented for '{provider}'")

    return {"success": True, "data": {"url": url, "state": state}}


# ---------------------------------------------------------------------------
# Shared OAuth token exchange (used by both GET and POST callbacks)
# ---------------------------------------------------------------------------

async def _exchange_oauth_code(provider: str, code: str) -> dict:
    """Exchange an OAuth authorization code for credentials. Raises ValueError on failure."""
    async with httpx.AsyncClient() as client:
        if provider == "github":
            resp = await client.post(
                "https://github.com/login/oauth/access_token",
                data={
                    "client_id": settings.github_client_id,
                    "client_secret": settings.github_client_secret,
                    "code": code,
                },
                headers={"Accept": "application/json"},
            )
            data = resp.json() if resp.status_code == 200 else {}
            if "access_token" not in data:
                raise ValueError(f"GitHub token exchange failed: {data.get('error_description', data)}")
            return {"access_token": data["access_token"], "token_type": "bearer"}

        elif provider == "linear":
            resp = await client.post(
                "https://api.linear.app/oauth/token",
                data={
                    "client_id": settings.linear_client_id,
                    "client_secret": settings.linear_client_secret,
                    "redirect_uri": settings.linear_redirect_uri,
                    "code": code,
                    "grant_type": "authorization_code",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            data = resp.json() if resp.status_code == 200 else {}
            if "access_token" not in data:
                raise ValueError(f"Linear token exchange failed: {data}")
            return {"access_token": data["access_token"]}

        elif provider == "slack":
            resp = await client.post(
                "https://slack.com/api/oauth.v2.access",
                data={
                    "client_id": settings.slack_client_id,
                    "client_secret": settings.slack_client_secret,
                    "redirect_uri": settings.slack_redirect_uri,
                    "code": code,
                },
            )
            data = resp.json() if resp.status_code == 200 else {}
            if not data.get("ok"):
                raise ValueError(f"Slack token exchange failed: {data.get('error')}")
            return {
                "access_token": data.get("access_token"),
                "bot_token": data.get("access_token"),
                "team_id": data.get("team", {}).get("id"),
                "team_name": data.get("team", {}).get("name"),
                "incoming_webhook": data.get("incoming_webhook", {}).get("url"),
            }

        elif provider == "jira":
            resp = await client.post(
                "https://auth.atlassian.com/oauth/token",
                json={
                    "grant_type": "authorization_code",
                    "client_id": settings.jira_client_id,
                    "client_secret": settings.jira_client_secret,
                    "code": code,
                    "redirect_uri": settings.jira_redirect_uri,
                },
            )
            data = resp.json() if resp.status_code == 200 else {}
            if "access_token" not in data:
                raise ValueError(f"Jira token exchange failed: {data}")

            # Fetch the Atlassian cloud ID required for all API calls
            resources_resp = await client.get(
                "https://api.atlassian.com/oauth/token/accessible-resources",
                headers={"Authorization": f"Bearer {data['access_token']}"},
            )
            resources = resources_resp.json() if resources_resp.status_code == 200 else []
            cloud_id = resources[0]["id"] if resources else None
            cloud_url = resources[0]["url"] if resources else None

            return {
                "access_token": data["access_token"],
                "refresh_token": data.get("refresh_token"),
                "cloud_id": cloud_id,
                "cloud_url": cloud_url,
            }

        elif provider == "discord":
            resp = await client.post(
                "https://discord.com/api/oauth2/token",
                data={
                    "client_id": settings.discord_client_id,
                    "client_secret": settings.discord_client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": settings.discord_redirect_uri,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            data = resp.json() if resp.status_code == 200 else {}
            if "access_token" not in data:
                raise ValueError(f"Discord token exchange failed: {data}")
            guild = data.get("guild", {})
            return {
                "access_token": data["access_token"],
                "guild_id": guild.get("id", ""),
                "guild_name": guild.get("name", ""),
            }

        elif provider == "clickup":
            resp = await client.post(
                "https://api.clickup.com/api/v2/oauth/token",
                data={
                    "client_id": settings.clickup_client_id,
                    "client_secret": settings.clickup_client_secret,
                    "code": code,
                    "redirect_uri": settings.clickup_redirect_uri,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            data = resp.json() if resp.status_code == 200 else {}
            if "access_token" not in data:
                raise ValueError(f"ClickUp token exchange failed: {data}")
            return {"access_token": data["access_token"]}

        else:
            raise ValueError(f"OAuth not implemented for '{provider}'")


async def _upsert_integration(
    db: AsyncSession, tenant_id: str, provider: str, credentials: dict
) -> Integration:
    integration = await _get_integration(db, tenant_id, provider)
    if integration:
        integration.credentials = credentials
        integration.status = "active"
    else:
        integration = Integration(
            tenant_id=tenant_id,
            provider=provider,
            credentials=credentials,
            status="active",
        )
        db.add(integration)
    await db.commit()
    await db.refresh(integration)
    return integration


# ---------------------------------------------------------------------------
# OAuth — step 1 callback
# GET  = browser redirect from the provider (no auth cookie, tenant from Redis state)
# POST = API call from frontend after it receives the code (auth cookie present)
# ---------------------------------------------------------------------------

@router.get("/{provider}/callback")
async def oauth_callback_browser(
    provider: str,
    code: str,
    state: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Browser-facing callback — Slack/GitHub redirect here after the user authorises.
    Exchanges the code, saves credentials, then redirects to the frontend.
    Tenant is identified via the state token stored in Redis (no auth cookie needed).
    """
    if provider not in _CATALOG or _CATALOG[provider]["coming_soon"]:
        return RedirectResponse(
            url=f"{settings.frontend_url}/integrations?error=unsupported_provider"
        )

    # Resolve tenant from Redis state
    tenant_id: str | None = None
    try:
        r = await get_redis()
        tenant_id = await r.get(f"oauth_state:{state}")
        await r.delete(f"oauth_state:{state}")
    except Exception:
        pass

    if not tenant_id:
        return RedirectResponse(
            url=f"{settings.frontend_url}/integrations?error=invalid_state"
        )

    try:
        credentials = await _exchange_oauth_code(provider, code)
    except ValueError:
        return RedirectResponse(
            url=f"{settings.frontend_url}/integrations?error=oauth_failed&provider={provider}"
        )

    await _upsert_integration(db, tenant_id, provider, credentials)
    return RedirectResponse(
        url=f"{settings.frontend_url}/integrations?connected={provider}"
    )


@router.post("/{provider}/callback")
async def oauth_callback_api(
    provider: str,
    code: str,
    state: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    API-facing callback — frontend calls this after receiving the code from the redirect.
    Requires the user's auth cookie (current_user).
    """
    if provider not in _CATALOG or _CATALOG[provider]["coming_soon"]:
        raise HTTPException(status_code=400, detail=f"Provider '{provider}' not available")

    # Validate state
    try:
        r = await get_redis()
        stored_tenant = await r.get(f"oauth_state:{state}")
        if stored_tenant and stored_tenant != current_user.tenant_id:
            raise HTTPException(status_code=400, detail="Invalid OAuth state")
        await r.delete(f"oauth_state:{state}")
    except HTTPException:
        raise
    except Exception:
        pass

    try:
        credentials = await _exchange_oauth_code(provider, code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    integration = await _upsert_integration(db, current_user.tenant_id, provider, credentials)
    return {"success": True, "data": IntegrationResponse.model_validate(integration).model_dump()}


# ---------------------------------------------------------------------------
# Step 2: list available resources (repos, teams, channels)
# ---------------------------------------------------------------------------

@router.get("/github/repos")
async def list_github_repos(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List repositories accessible with the stored GitHub token."""
    integration = await _get_integration(db, current_user.tenant_id, "github")
    if not integration or not integration.credentials:
        raise HTTPException(status_code=404, detail="GitHub not connected")

    token = integration.credentials.get("access_token")
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.github.com/user/repos",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            params={"per_page": 100, "sort": "updated"},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Failed to fetch GitHub repos")

    repos = [
        {
            "id": r["id"],
            "full_name": r["full_name"],
            "private": r["private"],
            "description": r.get("description"),
            "updated_at": r.get("updated_at"),
        }
        for r in resp.json()
    ]
    return {"success": True, "data": repos}


@router.get("/linear/teams")
async def list_linear_teams(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List Linear teams accessible with the stored Linear token."""
    integration = await _get_integration(db, current_user.tenant_id, "linear")
    if not integration or not integration.credentials:
        raise HTTPException(status_code=404, detail="Linear not connected")

    token = integration.credentials.get("access_token")
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.linear.app/graphql",
            json={"query": "{ teams { nodes { id name description } } }"},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Failed to fetch Linear teams")

    data = resp.json()
    teams = data.get("data", {}).get("teams", {}).get("nodes", [])
    return {"success": True, "data": teams}


@router.get("/slack/channels")
async def list_slack_channels(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List public Slack channels accessible with the stored bot token."""
    integration = await _get_integration(db, current_user.tenant_id, "slack")
    if not integration or not integration.credentials:
        raise HTTPException(status_code=404, detail="Slack not connected")

    token = integration.credentials.get("bot_token") or integration.credentials.get("access_token")
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://slack.com/api/conversations.list",
            headers={"Authorization": f"Bearer {token}"},
            params={"limit": 200, "exclude_archived": "true"},
        )
    data = resp.json() if resp.status_code == 200 else {}
    if not data.get("ok"):
        raise HTTPException(status_code=502, detail="Failed to fetch Slack channels")

    channels = [
        {"id": c["id"], "name": c["name"], "is_private": c.get("is_private", False)}
        for c in data.get("channels", [])
    ]
    return {"success": True, "data": channels}


@router.get("/jira/projects")
async def list_jira_projects(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List Jira projects accessible with the stored OAuth token."""
    integration = await _get_integration(db, current_user.tenant_id, "jira")
    if not integration or not integration.credentials:
        raise HTTPException(status_code=404, detail="Jira not connected")

    token = integration.credentials.get("access_token")
    cloud_id = integration.credentials.get("cloud_id")
    if not cloud_id:
        raise HTTPException(status_code=400, detail="Jira cloud ID missing — reconnect the integration")

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/project/search",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params={"maxResults": 50},
        )
    data = resp.json() if resp.status_code == 200 else {}
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Failed to fetch Jira projects")

    projects = [
        {"id": p["id"], "key": p["key"], "name": p["name"]}
        for p in data.get("values", [])
    ]
    return {"success": True, "data": projects}


@router.get("/clickup/lists")
async def list_clickup_lists(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all ClickUp lists across all accessible workspaces and spaces."""
    integration = await _get_integration(db, current_user.tenant_id, "clickup")
    if not integration or not integration.credentials:
        raise HTTPException(status_code=404, detail="ClickUp not connected")

    token = integration.credentials.get("access_token")
    headers = {"Authorization": token, "Content-Type": "application/json"}
    all_lists: list[dict] = []

    async with httpx.AsyncClient() as client:
        teams_resp = await client.get("https://api.clickup.com/api/v2/team", headers=headers)
        if teams_resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Failed to fetch ClickUp workspaces")

        teams = teams_resp.json().get("teams", [])
        for team in teams:
            team_id = team["id"]
            team_name = team["name"]
            spaces_resp = await client.get(
                f"https://api.clickup.com/api/v2/team/{team_id}/space",
                headers=headers,
                params={"archived": "false"},
            )
            if spaces_resp.status_code != 200:
                continue
            spaces = spaces_resp.json().get("spaces", [])
            for space in spaces:
                space_id = space["id"]
                space_name = space["name"]
                lists_resp = await client.get(
                    f"https://api.clickup.com/api/v2/space/{space_id}/list",
                    headers=headers,
                    params={"archived": "false"},
                )
                if lists_resp.status_code != 200:
                    continue
                for lst in lists_resp.json().get("lists", []):
                    all_lists.append({
                        "id": lst["id"],
                        "name": lst["name"],
                        "space_name": space_name,
                        "team_name": team_name,
                    })

    return {"success": True, "data": all_lists}


@router.get("/discord/channels")
async def list_discord_channels(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List text channels in the connected Discord guild."""
    integration = await _get_integration(db, current_user.tenant_id, "discord")
    if not integration or not integration.credentials:
        raise HTTPException(status_code=404, detail="Discord not connected")

    guild_id = integration.credentials.get("guild_id")
    bot_token = settings.discord_bot_token
    if not guild_id or not bot_token:
        raise HTTPException(status_code=400, detail="Discord guild or bot token not configured")

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://discord.com/api/v10/guilds/{guild_id}/channels",
            headers={"Authorization": f"Bot {bot_token}"},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Failed to fetch Discord channels")

    # type 0 = GUILD_TEXT
    channels = [
        {"id": c["id"], "name": c["name"]}
        for c in resp.json()
        if c.get("type") == 0
    ]
    return {"success": True, "data": channels}


# ---------------------------------------------------------------------------
# Step 3: save configuration (repos/teams/channels + routing rules)
# ---------------------------------------------------------------------------

@router.post("/{provider}/configure")
async def configure_integration(
    provider: str,
    body: IntegrationConfigureRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Persist the user's selections from wizard steps 2+3:
    - repos/teams/channels to sync
    - optional routing rules
    Updates integration.config and creates routing rules.
    """
    integration = await _get_integration(db, current_user.tenant_id, provider)
    if not integration:
        raise HTTPException(status_code=404, detail=f"{provider} integration not found")

    # Build meta stats from selections
    selections = body.selections
    meta: dict = dict(integration.config or {})

    if provider == "github":
        repos = selections.get("repos", [])
        meta["active_repos"] = len(repos)
        meta["repo_names"] = repos

        # Generate a webhook secret once and register hooks on every configured repo
        webhook_secret = integration.webhook_secret or secrets.token_hex(32)
        integration.webhook_secret = webhook_secret

        access_token = (integration.credentials or {}).get("access_token")
        backend_root = (
            settings.backend_url
            or settings.allowed_origins
            or "http://localhost:8000"
        )
        hook_url = f"{backend_root}/v1/webhooks/github/{current_user.tenant_id}"

        if access_token:
            async with httpx.AsyncClient() as client:
                headers = {
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                }
                for repo in repos:
                    existing_resp = await client.get(
                        f"https://api.github.com/repos/{repo}/hooks",
                        headers=headers,
                        params={"per_page": 100},
                        timeout=10.0,
                    )
                    existing_hooks = existing_resp.json() if existing_resp.status_code == 200 else []
                    existing_hook = next(
                        (h for h in existing_hooks if h.get("config", {}).get("url") == hook_url),
                        None,
                    )
                    hook_config = {
                        "name": "web",
                        "active": True,
                        "events": ["issues"],
                        "config": {
                            "url": hook_url,
                            "content_type": "json",
                            "secret": webhook_secret,
                        },
                    }
                    if existing_hook:
                        # Always patch to keep the secret in sync with the DB value
                        await client.patch(
                            f"https://api.github.com/repos/{repo}/hooks/{existing_hook['id']}",
                            headers=headers,
                            json=hook_config,
                            timeout=10.0,
                        )
                    else:
                        await client.post(
                            f"https://api.github.com/repos/{repo}/hooks",
                            headers=headers,
                            json=hook_config,
                            timeout=10.0,
                        )

        meta["webhook_url"] = hook_url

    elif provider == "linear":
        teams = selections.get("teams", [])
        meta["mapped_teams"] = len(teams)
        meta["team_ids"] = [t.get("id") if isinstance(t, dict) else t for t in teams]
    elif provider == "slack":
        channels = selections.get("channels", [])
        meta["active_channels"] = len(channels)
        meta["channel_ids"] = [c.get("id") if isinstance(c, dict) else c for c in channels]
    elif provider == "jira":
        projects = selections.get("projects", [])
        meta["project_keys"] = [p.get("key") if isinstance(p, dict) else p for p in projects]
        meta["active_projects"] = len(projects)
    elif provider == "discord":
        channels = selections.get("channels", [])
        meta["channel_ids"] = [c.get("id") if isinstance(c, dict) else c for c in channels]
        meta["active_channels"] = len(channels)
        # Allow users to provide an Incoming Webhook URL as an alternative to bot-token routing
        webhook_url = selections.get("webhook_url")
        if webhook_url:
            creds = dict(integration.credentials or {})
            creds["webhook_url"] = webhook_url
            integration.credentials = creds
    elif provider == "clickup":
        lists = selections.get("lists", [])
        meta["list_ids"] = [lst.get("id") if isinstance(lst, dict) else lst for lst in lists]
        meta["list_names"] = [lst.get("name", "") if isinstance(lst, dict) else lst for lst in lists]
        meta["active_lists"] = len(lists)

        token = (integration.credentials or {}).get("access_token")
        backend_base = settings.backend_url or "http://localhost:8000"
        webhook_endpoint = f"{backend_base}/v1/webhooks/clickup/{current_user.tenant_id}"

        if token and backend_base:
            async with httpx.AsyncClient(timeout=10.0) as client:
                headers = {"Authorization": token}

                # Resolve workspace/team ID
                teams_resp = await client.get("https://api.clickup.com/api/v2/team", headers=headers)
                teams = teams_resp.json().get("teams", []) if teams_resp.status_code == 200 else []

                if teams:
                    team_id = teams[0]["id"]

                    # Delete previous webhook registration so we don't accumulate stale hooks
                    old_webhook_id = meta.get("clickup_webhook_id")
                    if old_webhook_id:
                        await client.delete(
                            f"https://api.clickup.com/api/v2/webhook/{old_webhook_id}",
                            headers=headers,
                        )

                    # Register the inbound webhook for taskStatusUpdated events
                    hook_resp = await client.post(
                        f"https://api.clickup.com/api/v2/team/{team_id}/webhook",
                        headers={**headers, "Content-Type": "application/json"},
                        json={"endpoint": webhook_endpoint, "events": ["taskStatusUpdated"]},
                    )
                    if hook_resp.status_code == 200:
                        hook_data = hook_resp.json()
                        webhook_info = hook_data.get("webhook", {})
                        secret = webhook_info.get("secret", "")
                        webhook_id = webhook_info.get("id", "")
                        if secret:
                            creds = dict(integration.credentials or {})
                            creds["webhook_secret"] = secret
                            integration.credentials = creds
                        meta["clickup_webhook_id"] = webhook_id
                        meta["clickup_webhook_url"] = webhook_endpoint

    integration.config = meta

    # Persist any routing rules included in step 3
    for rule_data in body.routing_rules:
        rule = RoutingRule(
            tenant_id=current_user.tenant_id,
            condition=rule_data.get("condition", {}),
            action_type=rule_data.get("action_type", ""),
            action_config=rule_data.get("action_config", {}),
        )
        db.add(rule)

    await db.commit()
    await db.refresh(integration)
    return {"success": True, "data": IntegrationResponse.model_validate(integration).model_dump()}


# ---------------------------------------------------------------------------
# Disconnect
# ---------------------------------------------------------------------------

@router.delete("/{provider}")
async def disconnect_integration(
    provider: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Remove a connected integration and its credentials."""
    integration = await _get_integration(db, current_user.tenant_id, provider)
    if not integration:
        raise HTTPException(status_code=404, detail=f"{provider} integration not found")

    await db.delete(integration)
    await db.commit()
    return {"success": True, "message": f"{provider} disconnected"}


# ---------------------------------------------------------------------------
# Raw create (internal / webhook-based connections)
# ---------------------------------------------------------------------------

@router.post("", response_model=IntegrationResponse)
async def create_integration(
    body: IntegrationCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tenant_id = current_user.tenant_id
    integration = Integration(
        tenant_id=tenant_id,
        provider=body.provider,
        credentials=body.credentials,
    )
    db.add(integration)
    await db.commit()
    await db.refresh(integration)
    return integration


# ---------------------------------------------------------------------------
# Routing rules
# ---------------------------------------------------------------------------

@router.post("/routing-rules", response_model=RoutingRuleResponse)
async def create_routing_rule(
    body: RoutingRuleCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tenant_id = current_user.tenant_id
    rule = RoutingRule(
        tenant_id=tenant_id,
        name=body.name,
        enabled=body.enabled,
        condition=body.condition,
        action_type=body.action_type,
        action_config=body.action_config,
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return rule


@router.get("/routing-rules")
async def list_routing_rules(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tenant_id = current_user.tenant_id
    result = await db.execute(
        select(RoutingRule)
        .where(RoutingRule.tenant_id == tenant_id)
        .order_by(RoutingRule.created_at)
    )
    items = result.scalars().all()
    return {
        "success": True,
        "message": [RoutingRuleResponse.model_validate(i).model_dump() for i in items],
    }


@router.patch("/routing-rules/{rule_id}", response_model=RoutingRuleResponse)
async def update_routing_rule(
    rule_id: str,
    body: RoutingRuleUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(RoutingRule).where(
            RoutingRule.id == rule_id,
            RoutingRule.tenant_id == current_user.tenant_id,
        )
    )
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="Routing rule not found")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(rule, field, value)

    await db.commit()
    await db.refresh(rule)
    return rule


@router.delete("/routing-rules/{rule_id}")
async def delete_routing_rule(
    rule_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(RoutingRule).where(
            RoutingRule.id == rule_id,
            RoutingRule.tenant_id == current_user.tenant_id,
        )
    )
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="Routing rule not found")

    await db.delete(rule)
    await db.commit()
    return {"success": True}
