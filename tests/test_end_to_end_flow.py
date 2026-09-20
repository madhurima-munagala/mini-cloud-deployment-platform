"""
Stage 10 — final end-to-end integration & validation.

These tests drive the WHOLE backend through its real HTTP surface only:

    frontend-facing   /api/v1/...              (session-cookie auth)
    engine callbacks  /internal/engine-callback/{status,logs,metrics}
                                                (X-Internal-Token auth)

Nothing below reaches into services or the ORM to make the flow happen.
The only things replaced are the two EXTERNAL boundaries:

- GitHub: github_repository_service.list_repositories_for_user is
  monkeypatched (same pattern as tests/test_repositories.py).
- The deployment engine: deployment_engine_client.trigger_deployment is
  monkeypatched with a recording spy (same pattern as
  tests/test_deployments.py). The spy captures exactly what the backend
  would have POSTed to {ENGINE_URL}/deploy, and the "engine" side of the
  conversation is played by this file calling the /internal/ callback
  routes directly with the shared-secret header.

Everything else is the real application: routing, auth, ownership checks,
env-var resolution/encryption, the BackgroundTasks engine trigger, the
callback services and the read endpoints, all against the real PostgreSQL
test database.

Reuses the SAVEPOINT-based app_db_session/authed_client/client fixtures
from tests/test_auth.py (imported, not duplicated) and the make_user
factory from tests/conftest.py. Neither of those files, nor any
application file, nor API_CONTRACT.md, is modified by this stage.

Timestamps: the test schema is built from the ORM models (naive
DateTime columns), so the exact wall-clock value read back can depend on
the database session's TimeZone. These tests therefore never compare a
read-back timestamp against a value they sent; they only compare
timestamps to OTHER timestamps returned by the API (ordering, and echoing
a returned timestamp back as `since`), which is timezone-independent.

Contract-compliance fixes applied after Stage 10 are asserted here too:
the 409 DEPLOYMENT_IN_PROGRESS `details.existing_deployment_id`, the
`repository_name` on deployment responses, the `meta` object on
GET /deployments, and the REPO_NOT_FOUND error code.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import settings
from app.services import (
    deployment_engine_client,
    environment_variable_service,
    github_repository_service,
    session_service,
)
from app.utils.encryption import decrypt_value, encrypt_value
from tests.conftest import make_user
from tests.test_auth import app_db_session, authed_client, client  # noqa: F401

TEST_TOKEN = "e2e-internal-token"
ENGINE_URL = "http://engine.internal:9000"
INTERNAL_HEADERS = {"X-Internal-Token": TEST_TOKEN}

MASK = "\u2022" * 8
LIVE_URL = "http://ec2-13-234-1-1.compute.amazonaws.com:8421"

# Fixed, whole-second base so every sample/log line has a distinct,
# predictable position on the timeline.
BASE_TIME = datetime(2026, 9, 8, 10, 15, 0, tzinfo=timezone.utc)

# API_CONTRACT.md §1: error code for "repository does not exist / not
# owned". Kept in ONE place so any future change only touches this constant.
REPOSITORY_NOT_FOUND_CODE = "REPO_NOT_FOUND"

# API_CONTRACT.md §9.1: message recorded when the engine can't be reached.
ENGINE_UNREACHABLE_MESSAGE = "Deployment engine unreachable"


# ---------------------------------------------------------------------
# Engine boundary (external) — recording spy
# ---------------------------------------------------------------------


class EngineSpy:
    """Stands in for deployment_engine_client.trigger_deployment().

    Records every call the backend makes to the engine. Set `.error` to an
    exception instance to simulate the engine being unavailable.
    """

    def __init__(self):
        self.calls: list[dict] = []
        self.error: Exception | None = None

    async def trigger(self, deployment_id, *, clone_url, branch, env_vars):
        self.calls.append(
            {
                "deployment_id": deployment_id,
                "clone_url": clone_url,
                "branch": branch,
                "env_vars": list(env_vars),
            }
        )
        if self.error is not None:
            raise self.error
        return f"job_{len(self.calls)}"


@pytest.fixture()
def engine_spy(app_db_session, monkeypatch):
    """Configures the internal channel and replaces the engine boundary.

    - engine_base_url / internal_api_token: set so the trigger and the
      /internal/ callback routes are "configured", exactly as in prod.
    - trigger_deployment: replaced by the recording spy above.
    - SessionLocal (used by run_engine_trigger's background task, which
      opens its OWN session): redirected to the isolated test session,
      for the same reason as tests/test_deployments.py tests 20-31.
    """
    monkeypatch.setattr(settings, "engine_base_url", ENGINE_URL)
    monkeypatch.setattr(settings, "internal_api_token", TEST_TOKEN)

    spy = EngineSpy()
    monkeypatch.setattr(deployment_engine_client, "trigger_deployment", spy.trigger)
    monkeypatch.setattr(deployment_engine_client, "SessionLocal", lambda: app_db_session)
    return spy


# ---------------------------------------------------------------------
# GitHub boundary (external), users, login
# ---------------------------------------------------------------------


def _github_repo(*, github_repo_id: int, name: str = "weather-app", default_branch: str = "main") -> dict:
    return {
        "github_repo_id": github_repo_id,
        "name": name,
        "full_name": f"e2e-user/{name}",
        "private": False,
        "default_branch": default_branch,
        "clone_url": f"https://github.com/e2e-user/{name}.git",
        "github_updated_at": datetime.now(timezone.utc),
    }


def _mock_github(monkeypatch, repos_by_token: dict[str, list[dict]]) -> None:
    """GitHub returns a per-access-token repo list, so a test with two users
    also proves each user's OWN token is the one used."""

    async def _fake_list(access_token, *, page, per_page):
        return repos_by_token[access_token]

    monkeypatch.setattr(github_repository_service, "list_repositories_for_user", _fake_list)


def _make_user_session(app_db_session, *, github_id: int, token: str) -> str:
    """Creates a user (with an encrypted GitHub access token) plus a login
    session for them, and returns ONLY the raw session-cookie value.

    Deliberately returns a plain string, never the ORM `User`: the
    engine-trigger background task closes the shared test session (see the
    engine_spy fixture), which detaches every ORM instance created before
    it — and, after any commit, those instances are also expired, so
    touching one later (e.g. to log in a second user mid-test) would raise
    DetachedInstanceError. A cookie value is immune to that.
    """
    user = make_user(app_db_session, github_id=github_id)
    user.github_access_token_encrypted = encrypt_value(token)
    app_db_session.flush()
    raw_token, _session = session_service.create_session(app_db_session, user)
    return raw_token


def _use_session(client, raw_session_token: str) -> None:
    """Switches the shared TestClient to the given user's session cookie."""
    client.cookies.clear()
    client.cookies.set(settings.session_cookie_name, raw_session_token)


# ---------------------------------------------------------------------
# Small HTTP helpers (all go through the real routes)
# ---------------------------------------------------------------------


def _ts(offset_seconds: int) -> str:
    return (BASE_TIME + timedelta(seconds=offset_seconds)).isoformat()


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _assert_error(response, status_code: int, code: str) -> None:
    assert response.status_code == status_code, response.text
    body = response.json()
    assert list(body.keys()) == ["error"]
    assert set(body["error"].keys()) == {"code", "message", "details"}
    assert body["error"]["code"] == code


def _list_repositories(client) -> list[dict]:
    response = client.get("/api/v1/repositories")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _create_deployment(client, repository_id: str, *, env_vars: list[dict] | None = None) -> dict:
    body: dict = {"repository_id": repository_id}
    if env_vars is not None:
        body["env_vars"] = env_vars
    response = client.post("/api/v1/deployments", json=body)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _list_deployments(client, **params) -> list[dict]:
    response = client.get("/api/v1/deployments", params=params)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _get_deployment(client, deployment_id: str) -> dict:
    response = client.get(f"/api/v1/deployments/{deployment_id}")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _get_logs(client, deployment_id: str, **params) -> list[dict]:
    response = client.get(f"/api/v1/deployments/{deployment_id}/logs", params=params)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _get_metrics(client, deployment_id: str, **params) -> dict:
    response = client.get(f"/api/v1/deployments/{deployment_id}/metrics", params=params)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _get_env(client, repository_id: str) -> list[dict]:
    response = client.get(f"/api/v1/repositories/{repository_id}/env")
    assert response.status_code == 200, response.text
    return response.json()["data"]


# --- the "engine" side: calls the real /internal/ callback routes ---


def _post_status(
    client,
    deployment_id: str,
    status: str,
    *,
    offset: int,
    live_url: str | None = None,
    container_id: str | None = None,
    error_message: str | None = None,
) -> None:
    response = client.post(
        "/internal/engine-callback/status",
        json={
            "deployment_id": str(deployment_id),
            "status": status,
            "live_url": live_url,
            "container_id": container_id,
            "error_message": error_message,
            "timestamp": _ts(offset),
        },
        headers=INTERNAL_HEADERS,
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"received": True}


def _post_logs(client, deployment_id: str, entries: list[tuple[int, str, str]]) -> None:
    """entries: (timestamp offset in seconds, level, message)."""
    response = client.post(
        "/internal/engine-callback/logs",
        json={
            "deployment_id": str(deployment_id),
            "logs": [
                {"timestamp": _ts(offset), "level": level, "message": message}
                for offset, level, message in entries
            ],
        },
        headers=INTERNAL_HEADERS,
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"received": True, "count": len(entries)}


def _post_metric(
    client,
    deployment_id: str,
    *,
    offset: int,
    cpu: float,
    memory: float,
    container_status: str = "running",
) -> None:
    response = client.post(
        "/internal/engine-callback/metrics",
        json={
            "deployment_id": str(deployment_id),
            "container_status": container_status,
            "cpu_percent": cpu,
            "memory_mb": memory,
            "timestamp": _ts(offset),
        },
        headers=INTERNAL_HEADERS,
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"received": True}


def _bootstrap_running_deployment(
    client, app_db_session, monkeypatch, *, github_id: int, github_repo_id: int
) -> str:
    """user -> login -> repository (via GitHub sync) -> deployment (via the
    API + engine spy) -> engine reports `running`. Returns deployment id."""
    token = f"gh-token-{github_id}"
    _use_session(client, _make_user_session(app_db_session, github_id=github_id, token=token))
    _mock_github(
        monkeypatch, {token: [_github_repo(github_repo_id=github_repo_id, name="polling-app")]}
    )

    repository = _list_repositories(client)[0]
    deployment = _create_deployment(client, repository["id"])
    _post_status(
        client, deployment["id"], "running", offset=0, live_url=LIVE_URL, container_id="c_poll"
    )
    return deployment["id"]


# --- frontend-style pollers (§4.4 / §7.1: since=<last seen>, de-duplicate) ---


class _LogPoller:
    """Mimics the frontend's log polling: pass `since=<last timestamp seen>`,
    de-duplicate by the log line's `id` (the reason DeploymentLogOut exposes
    it), and accumulate what has been seen so far."""

    def __init__(self, client, deployment_id: str, *, limit: int | None = None):
        self.client = client
        self.deployment_id = deployment_id
        self.limit = limit
        self.since: str | None = None
        self.seen_ids: set[int] = set()
        self.collected: list[dict] = []

    def poll(self) -> list[dict]:
        params: dict = {}
        if self.since is not None:
            params["since"] = self.since
        if self.limit is not None:
            params["limit"] = self.limit

        entries = _get_logs(self.client, self.deployment_id, **params)

        new = [entry for entry in entries if entry["id"] not in self.seen_ids]
        for entry in new:
            self.seen_ids.add(entry["id"])
        self.collected.extend(new)
        if entries:
            self.since = entries[-1]["timestamp"]
        return new


class _MetricsPoller:
    """Same idea for metrics. Metric points have no id, so a point is
    identified by its (distinct-per-test) timestamp string."""

    def __init__(self, client, deployment_id: str, *, limit: int | None = None):
        self.client = client
        self.deployment_id = deployment_id
        self.limit = limit
        self.since: str | None = None
        self.seen_timestamps: set[str] = set()
        self.collected: list[dict] = []
        self.latest_cpu_per_poll: list[float] = []

    def poll(self) -> list[dict]:
        params: dict = {}
        if self.since is not None:
            params["since"] = self.since
        if self.limit is not None:
            params["limit"] = self.limit

        data = _get_metrics(self.client, self.deployment_id, **params)
        self.latest_cpu_per_poll.append(data["latest"]["cpu_percent"])

        history = data["history"]
        new = [point for point in history if point["timestamp"] not in self.seen_timestamps]
        for point in new:
            self.seen_timestamps.add(point["timestamp"])
        self.collected.extend(new)
        if history:
            self.since = history[-1]["timestamp"]
        return new


def _messages(entries: list[dict]) -> list[str]:
    return [entry["message"] for entry in entries]


# =====================================================================
# 1. Full happy path
# =====================================================================


def test_e2e_happy_path_repository_to_deployment_logs_and_metrics(
    authed_client, app_db_session, engine_spy, monkeypatch
):
    _use_session(
        authed_client,
        _make_user_session(app_db_session, github_id=12001, token="gh-token-happy"),
    )
    _mock_github(
        monkeypatch,
        {"gh-token-happy": [_github_repo(github_repo_id=91001, default_branch="develop")]},
    )

    # --- repository: synced from (mocked) GitHub through GET /repositories ---
    repositories = _list_repositories(authed_client)
    assert len(repositories) == 1
    repository = repositories[0]
    assert repository["name"] == "weather-app"
    assert repository["private"] is False
    assert repository["default_branch"] == "develop"
    assert repository["clone_url"] == "https://github.com/e2e-user/weather-app.git"
    repository_id = repository["id"]

    # --- repository-level env defaults (PUT), masked on the way back ---
    put_response = authed_client.put(
        f"/api/v1/repositories/{repository_id}/env",
        json={
            "env_vars": [
                {"key": "PORT", "value": "3000"},
                {"key": "NODE_ENV", "value": "production"},
                {"key": "API_KEY", "value": "sk_live_default"},
            ]
        },
    )
    assert put_response.status_code == 200, put_response.text
    assert {item["key"] for item in put_response.json()["data"]} == {"PORT", "NODE_ENV", "API_KEY"}
    assert all(item["value"] == MASK for item in put_response.json()["data"])
    assert "sk_live_default" not in put_response.text

    assert [(item["key"], item["value"]) for item in _get_env(authed_client, repository_id)] == [
        ("API_KEY", MASK),
        ("NODE_ENV", MASK),
        ("PORT", MASK),
    ]

    # --- deployment with a per-deployment override (PORT, API_KEY) ---
    create_response = authed_client.post(
        "/api/v1/deployments",
        json={
            "repository_id": repository_id,
            "env_vars": [
                {"key": "PORT", "value": "8080"},
                {"key": "API_KEY", "value": "sk_live_override"},
            ],
        },
    )
    assert create_response.status_code == 201, create_response.text
    created = create_response.json()["data"]
    deployment_id = created["id"]
    assert created["repository_id"] == repository_id
    assert created["branch"] == "develop"  # omitted -> repo default_branch
    assert created["status"] == "pending"
    assert created["live_url"] is None
    assert "sk_live_default" not in create_response.text
    assert "sk_live_override" not in create_response.text

    # --- engine trigger: what the backend would have POSTed to /deploy ---
    assert len(engine_spy.calls) == 1
    call = engine_spy.calls[0]
    assert str(call["deployment_id"]) == deployment_id
    assert call["clone_url"] == "https://github.com/e2e-user/weather-app.git"
    assert call["branch"] == "develop"
    # Effective set: override wins for PORT/API_KEY, default fills NODE_ENV.
    # Plaintext travels only on this internal channel.
    assert call["env_vars"] == [
        {"key": "API_KEY", "value": "sk_live_override"},
        {"key": "NODE_ENV", "value": "production"},
        {"key": "PORT", "value": "8080"},
    ]

    # Trigger accepted -> the backend leaves the deployment `pending` until
    # the engine reports otherwise.
    assert _get_deployment(authed_client, deployment_id)["status"] == "pending"

    # Concurrency rule: a second deploy of the same repo is rejected while
    # the first is active, and never reaches the engine. The 409 names the
    # active deployment (API_CONTRACT.md §4.1).
    conflict = authed_client.post("/api/v1/deployments", json={"repository_id": repository_id})
    _assert_error(conflict, 409, "DEPLOYMENT_IN_PROGRESS")
    assert conflict.json()["error"]["details"] == {"existing_deployment_id": deployment_id}
    assert len(engine_spy.calls) == 1

    # Nothing has been reported yet.
    assert _get_logs(authed_client, deployment_id) == []
    _assert_error(
        authed_client.get(f"/api/v1/deployments/{deployment_id}/metrics"),
        404,
        "METRICS_NOT_AVAILABLE",
    )

    # The callback channel is authenticated: no token -> 401, state untouched.
    unauthenticated_callback = authed_client.post(
        "/internal/engine-callback/status",
        json={"deployment_id": deployment_id, "status": "running", "timestamp": _ts(0)},
    )
    _assert_error(unauthenticated_callback, 401, "INTERNAL_TOKEN_INVALID")
    assert _get_deployment(authed_client, deployment_id)["status"] == "pending"

    # --- engine: building ---
    _post_status(authed_client, deployment_id, "building", offset=5)
    assert _get_deployment(authed_client, deployment_id)["status"] == "building"

    _post_logs(
        authed_client,
        deployment_id,
        [(3, "info", "Cloning repository..."), (10, "info", "Building Docker image...")],
    )
    logs = _get_logs(authed_client, deployment_id)
    assert _messages(logs) == ["Cloning repository...", "Building Docker image..."]
    assert [entry["level"] for entry in logs] == ["info", "info"]
    assert all(isinstance(entry["id"], int) for entry in logs)

    # --- engine: starting ---
    _post_status(authed_client, deployment_id, "starting", offset=100)
    assert _get_deployment(authed_client, deployment_id)["status"] == "starting"

    _post_logs(
        authed_client,
        deployment_id,
        [(101, "warn", "Deprecated dependency: left-pad"), (102, "info", "Starting container...")],
    )
    logs = _get_logs(authed_client, deployment_id)
    assert _messages(logs) == [
        "Cloning repository...",
        "Building Docker image...",
        "Deprecated dependency: left-pad",
        "Starting container...",
    ]
    assert [entry["level"] for entry in logs] == ["info", "info", "warn", "info"]

    # --- engine: running (live_url + container_id delivered on the status callback) ---
    _post_status(
        authed_client,
        deployment_id,
        "running",
        offset=120,
        live_url=LIVE_URL,
        container_id="c_92be1a",
    )
    detail = _get_deployment(authed_client, deployment_id)
    assert detail["id"] == deployment_id
    assert detail["repository_id"] == repository_id
    assert detail["repository_name"] == "weather-app"
    assert detail["branch"] == "develop"
    assert detail["status"] == "running"
    assert detail["live_url"] == LIVE_URL
    assert detail["container_id"] == "c_92be1a"
    assert detail["error_message"] is None

    # --- engine: metrics (one point per callback) ---
    _post_metric(authed_client, deployment_id, offset=130, cpu=10.1, memory=172.0)
    _post_metric(authed_client, deployment_id, offset=190, cpu=12.4, memory=184.2)
    _post_metric(authed_client, deployment_id, offset=250, cpu=15.8, memory=190.5)

    metrics = _get_metrics(authed_client, deployment_id)
    assert metrics["deployment_id"] == deployment_id
    assert metrics["container_status"] == "running"
    assert metrics["latest"]["cpu_percent"] == 15.8
    assert metrics["latest"]["memory_mb"] == 190.5
    assert [point["cpu_percent"] for point in metrics["history"]] == [10.1, 12.4, 15.8]
    assert [point["memory_mb"] for point in metrics["history"]] == [172.0, 184.2, 190.5]
    assert metrics["latest"]["timestamp"] == metrics["history"][-1]["timestamp"]

    # --- deployment history reflects the final state ---
    listing = _list_deployments(authed_client)
    assert [item["id"] for item in listing] == [deployment_id]
    assert listing[0]["status"] == "running"
    assert listing[0]["live_url"] == LIVE_URL
    assert listing[0]["repository_name"] == "weather-app"
    listing_response = authed_client.get("/api/v1/deployments")
    assert listing_response.json()["meta"] == {"page": 1, "per_page": 20, "has_next": False}

    filtered = _list_deployments(authed_client, repository_id=repository_id, status="running")
    assert [item["id"] for item in filtered] == [deployment_id]
    assert _list_deployments(authed_client, status="failed") == []

    # --- the deployment override never rewrote the repository defaults ---
    default_rows = environment_variable_service.list_repository_defaults(
        app_db_session, uuid.UUID(repository_id)
    )
    assert {row.key: decrypt_value(row.value_encrypted) for row in default_rows} == {
        "API_KEY": "sk_live_default",
        "NODE_ENV": "production",
        "PORT": "3000",
    }

    # --- plaintext env values never come back out of the frontend API ---
    for url in (
        "/api/v1/repositories",
        f"/api/v1/repositories/{repository_id}/env",
        "/api/v1/deployments",
        f"/api/v1/deployments/{deployment_id}",
        f"/api/v1/deployments/{deployment_id}/logs",
        f"/api/v1/deployments/{deployment_id}/metrics",
    ):
        body = authed_client.get(url).text
        assert "sk_live_default" not in body, url
        assert "sk_live_override" not in body, url


# =====================================================================
# 2. Full failure path — engine trigger failure
# =====================================================================


def test_e2e_engine_trigger_failure_marks_failed_with_failure_log(
    authed_client, app_db_session, engine_spy, monkeypatch
):
    _use_session(
        authed_client,
        _make_user_session(app_db_session, github_id=12011, token="gh-token-failure"),
    )
    _mock_github(
        monkeypatch, {"gh-token-failure": [_github_repo(github_repo_id=91011, name="broken-app")]}
    )
    repository_id = _list_repositories(authed_client)[0]["id"]

    engine_spy.error = deployment_engine_client.EngineUnavailableError("simulated engine outage")

    create_response = authed_client.post(
        "/api/v1/deployments",
        json={
            "repository_id": repository_id,
            "env_vars": [{"key": "API_KEY", "value": "sk_live_failpath"}],
        },
    )
    # The create request itself still succeeds — the trigger runs after the
    # response is built, so the body still shows the initial state.
    assert create_response.status_code == 201, create_response.text
    created = create_response.json()["data"]
    deployment_id = created["id"]
    assert created["status"] == "pending"

    # The engine was attempted exactly once, and did receive the plaintext.
    assert len(engine_spy.calls) == 1
    assert engine_spy.calls[0]["env_vars"] == [{"key": "API_KEY", "value": "sk_live_failpath"}]

    # --- deployment: failed, with the contract's error message ---
    detail_response = authed_client.get(f"/api/v1/deployments/{deployment_id}")
    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()["data"]
    assert detail["status"] == "failed"
    assert detail["error_message"] == ENGINE_UNREACHABLE_MESSAGE
    assert detail["live_url"] is None
    assert detail["container_id"] is None

    # --- logs: exactly one failure line, matching error_message ---
    logs_response = authed_client.get(f"/api/v1/deployments/{deployment_id}/logs")
    assert logs_response.status_code == 200, logs_response.text
    logs = logs_response.json()["data"]
    assert len(logs) == 1
    assert logs[0]["level"] == "error"
    assert logs[0]["message"] == ENGINE_UNREACHABLE_MESSAGE == detail["error_message"]

    # --- metrics: the container never started, so none exist ---
    _assert_error(
        authed_client.get(f"/api/v1/deployments/{deployment_id}/metrics"),
        404,
        "METRICS_NOT_AVAILABLE",
    )

    # --- history: visible under status=failed only ---
    assert [d["id"] for d in _list_deployments(authed_client, status="failed")] == [deployment_id]
    assert _list_deployments(authed_client, status="running") == []

    # --- the failure path never leaks the env values it was carrying ---
    assert "sk_live_failpath" not in detail_response.text
    assert "sk_live_failpath" not in logs_response.text

    # --- `failed` is terminal, not blocking: a retry is accepted. The
    #     override snapshot belonged to the first deployment only, so the
    #     retry's effective env set is empty. The engine is still down, so
    #     the retry fails too, with its own failure log. ---
    retry_id = _create_deployment(authed_client, repository_id)["id"]
    assert retry_id != deployment_id
    assert len(engine_spy.calls) == 2
    assert engine_spy.calls[1]["env_vars"] == []

    assert _get_deployment(authed_client, retry_id)["status"] == "failed"
    history = _list_deployments(authed_client, repository_id=repository_id)
    assert {d["id"] for d in history} == {deployment_id, retry_id}
    assert {d["status"] for d in history} == {"failed"}
    assert len(_get_logs(authed_client, deployment_id)) == 1
    assert len(_get_logs(authed_client, retry_id)) == 1


# =====================================================================
# 3. Cross-user isolation
# =====================================================================


def test_e2e_cross_user_isolation_across_deployments_logs_metrics_and_env(
    authed_client, app_db_session, engine_spy, monkeypatch
):
    session_a = _make_user_session(app_db_session, github_id=12101, token="gh-token-a")
    session_b = _make_user_session(app_db_session, github_id=12102, token="gh-token-b")
    _mock_github(
        monkeypatch,
        {
            "gh-token-a": [_github_repo(github_repo_id=91101, name="alpha")],
            "gh-token-b": [_github_repo(github_repo_id=91102, name="beta")],
        },
    )

    # --- user A: a complete, running deployment with logs, metrics and env ---
    _use_session(authed_client, session_a)
    repository_a = _list_repositories(authed_client)[0]
    put_response = authed_client.put(
        f"/api/v1/repositories/{repository_a['id']}/env",
        json={
            "env_vars": [
                {"key": "PORT", "value": "3000"},
                {"key": "API_KEY", "value": "sk_live_owner_secret"},
            ]
        },
    )
    assert put_response.status_code == 200, put_response.text
    deployment_a = _create_deployment(authed_client, repository_a["id"])["id"]
    _post_status(
        authed_client,
        deployment_a,
        "running",
        offset=10,
        live_url=LIVE_URL,
        container_id="c_alpha",
    )
    _post_logs(
        authed_client,
        deployment_a,
        [(1, "info", "alpha: cloning"), (2, "info", "alpha: build ok")],
    )
    _post_metric(authed_client, deployment_a, offset=20, cpu=11.0, memory=120.0)
    _post_metric(authed_client, deployment_a, offset=80, cpu=13.0, memory=125.0)

    # --- user B: their own, separate deployment ---
    _use_session(authed_client, session_b)
    repositories_b = _list_repositories(authed_client)
    assert [r["name"] for r in repositories_b] == ["beta"]  # never A's repo
    repository_b = repositories_b[0]
    assert repository_b["id"] != repository_a["id"]
    deployment_b = _create_deployment(authed_client, repository_b["id"])["id"]
    _post_logs(authed_client, deployment_b, [(1, "info", "beta: cloning")])
    _post_metric(authed_client, deployment_b, offset=20, cpu=1.0, memory=50.0)
    engine_calls_before_attack = len(engine_spy.calls)

    # --- B tries to reach A's deployment / logs / metrics: 404, never 403 ---
    missing_probe = authed_client.get(f"/api/v1/deployments/{uuid.uuid4()}")
    _assert_error(missing_probe, 404, "DEPLOYMENT_NOT_FOUND")
    for suffix in ("", "/logs", "/metrics"):
        _assert_error(
            authed_client.get(f"/api/v1/deployments/{deployment_a}{suffix}"),
            404,
            "DEPLOYMENT_NOT_FOUND",
        )
    # Indistinguishable from a deployment that doesn't exist at all.
    assert authed_client.get(f"/api/v1/deployments/{deployment_a}").json() == missing_probe.json()

    # --- B tries to read / overwrite A's repository env, or deploy A's repo ---
    _assert_error(
        authed_client.get(f"/api/v1/repositories/{repository_a['id']}/env"),
        404,
        REPOSITORY_NOT_FOUND_CODE,
    )
    _assert_error(
        authed_client.put(
            f"/api/v1/repositories/{repository_a['id']}/env",
            json={"env_vars": [{"key": "HIJACK", "value": "1"}]},
        ),
        404,
        REPOSITORY_NOT_FOUND_CODE,
    )
    _assert_error(
        authed_client.post("/api/v1/deployments", json={"repository_id": repository_a["id"]}),
        404,
        REPOSITORY_NOT_FOUND_CODE,
    )
    assert len(engine_spy.calls) == engine_calls_before_attack  # engine never contacted

    # --- B's own listings contain only B's data ---
    assert [d["id"] for d in _list_deployments(authed_client)] == [deployment_b]
    assert _list_deployments(authed_client, repository_id=repository_a["id"]) == []
    assert _get_env(authed_client, repository_b["id"]) == []  # A's defaults never bleed in
    assert _messages(_get_logs(authed_client, deployment_b)) == ["beta: cloning"]
    assert _get_metrics(authed_client, deployment_b)["latest"]["cpu_percent"] == 1.0

    # --- with no session at all, every user-facing read is 401 ---
    authed_client.cookies.clear()
    for url in (
        "/api/v1/deployments",
        f"/api/v1/deployments/{deployment_a}",
        f"/api/v1/deployments/{deployment_a}/logs",
        f"/api/v1/deployments/{deployment_a}/metrics",
        f"/api/v1/repositories/{repository_a['id']}/env",
    ):
        _assert_error(authed_client.get(url), 401, "NOT_AUTHENTICATED")

    # --- positive control: A's data is intact and still only A's ---
    _use_session(authed_client, session_a)
    assert [(i["key"], i["value"]) for i in _get_env(authed_client, repository_a["id"])] == [
        ("API_KEY", MASK),
        ("PORT", MASK),
    ]  # no HIJACK key
    detail_a = _get_deployment(authed_client, deployment_a)
    assert detail_a["status"] == "running"
    assert detail_a["container_id"] == "c_alpha"
    assert _messages(_get_logs(authed_client, deployment_a)) == ["alpha: cloning", "alpha: build ok"]
    metrics_a = _get_metrics(authed_client, deployment_a)
    assert metrics_a["latest"]["cpu_percent"] == 13.0
    assert len(metrics_a["history"]) == 2
    assert [d["id"] for d in _list_deployments(authed_client)] == [deployment_a]


# =====================================================================
# 4. Polling consistency (since / limit)
# =====================================================================


def test_e2e_polling_logs_since_has_no_gaps_or_duplicates_across_tied_timestamps(
    authed_client, app_db_session, engine_spy, monkeypatch
):
    deployment_id = _bootstrap_running_deployment(
        authed_client, app_db_session, monkeypatch, github_id=12201, github_repo_id=91201
    )
    poller = _LogPoller(authed_client, deployment_id)

    # Nothing logged yet: an empty poll is fine and reports nothing new.
    assert poller.poll() == []

    _post_logs(
        authed_client,
        deployment_id,
        [(1, "info", "line 1"), (2, "info", "line 2"), (3, "info", "line 3")],
    )
    assert _messages(poller.poll()) == ["line 1", "line 2", "line 3"]

    # Next batch: its first line carries the SAME timestamp as the last line
    # the poller already saw (the engine batches, so ties are realistic).
    # Polling from since=<last seen timestamp> must not lose it.
    _post_logs(
        authed_client,
        deployment_id,
        [(3, "warn", "line 4 (ties with line 3)"), (4, "info", "line 5"), (5, "error", "line 6")],
    )
    assert _messages(poller.poll()) == ["line 4 (ties with line 3)", "line 5", "line 6"]

    # Idempotent: polling again with nothing new reports nothing new, and
    # never re-reports lines already seen.
    assert poller.poll() == []

    ids = [entry["id"] for entry in poller.collected]
    assert len(ids) == len(set(ids)) == 6
    assert ids == sorted(ids)

    # What incremental polling accumulated equals one fresh full fetch.
    full = _get_logs(authed_client, deployment_id)
    assert _messages(full) == _messages(poller.collected)
    assert [entry["level"] for entry in full] == ["info", "info", "info", "warn", "info", "error"]


def test_e2e_polling_logs_limit_paging_drains_every_line_exactly_once(
    authed_client, app_db_session, engine_spy, monkeypatch
):
    deployment_id = _bootstrap_running_deployment(
        authed_client, app_db_session, monkeypatch, github_id=12211, github_repo_id=91211
    )
    _post_logs(
        authed_client,
        deployment_id,
        [(offset, "info", f"line {offset}") for offset in range(1, 6)],
    )

    poller = _LogPoller(authed_client, deployment_id, limit=2)

    # `limit` really bounds a single poll.
    first_poll = poller.poll()
    assert len(first_poll) == 2

    for _ in range(10):
        if not poller.poll():
            break
    else:
        pytest.fail("Polling logs with limit=2 never converged")

    assert _messages(poller.collected) == [f"line {n}" for n in range(1, 6)]
    ids = [entry["id"] for entry in poller.collected]
    assert len(ids) == len(set(ids)) == 5


def test_e2e_polling_metrics_since_and_limit_are_consistent(
    authed_client, app_db_session, engine_spy, monkeypatch
):
    deployment_id = _bootstrap_running_deployment(
        authed_client, app_db_session, monkeypatch, github_id=12221, github_repo_id=91221
    )

    # No samples yet: 404 METRICS_NOT_AVAILABLE, not an empty 200.
    _assert_error(
        authed_client.get(f"/api/v1/deployments/{deployment_id}/metrics"),
        404,
        "METRICS_NOT_AVAILABLE",
    )

    for index in range(3):
        _post_metric(
            authed_client,
            deployment_id,
            offset=10 * (index + 1),
            cpu=10.0 + index,
            memory=100.0 + 10 * index,
        )

    first = _get_metrics(authed_client, deployment_id)
    assert [p["cpu_percent"] for p in first["history"]] == [10.0, 11.0, 12.0]
    assert first["latest"]["cpu_percent"] == 12.0
    assert first["container_status"] == "running"
    last_seen = first["latest"]["timestamp"]
    seen_before = {point["timestamp"] for point in first["history"]}

    # Two more samples arrive; the newest reports a crash.
    _post_metric(authed_client, deployment_id, offset=40, cpu=13.0, memory=130.0)
    _post_metric(
        authed_client, deployment_id, offset=50, cpu=14.0, memory=140.0, container_status="crashed"
    )

    second = _get_metrics(authed_client, deployment_id, since=last_seen)
    # Nothing before `since` comes back; de-duplicating the boundary point
    # leaves exactly the two new samples, with no gap.
    assert all(_parse_ts(p["timestamp"]) >= _parse_ts(last_seen) for p in second["history"])
    new_points = [p for p in second["history"] if p["timestamp"] not in seen_before]
    assert [p["cpu_percent"] for p in new_points] == [13.0, 14.0]
    # `latest` / top-level container_status always describe the newest
    # sample, whatever `since` narrows history to.
    assert second["latest"]["cpu_percent"] == 14.0
    assert second["container_status"] == "crashed"

    # Paging the whole series with limit=2 yields every sample exactly once,
    # in order, while `latest` stays pinned to the newest sample throughout.
    poller = _MetricsPoller(authed_client, deployment_id, limit=2)
    first_poll = poller.poll()
    assert len(first_poll) == 2  # `limit` bounds a single poll

    for _ in range(10):
        if not poller.poll():
            break
    else:
        pytest.fail("Polling metrics with limit=2 never converged")

    assert [p["cpu_percent"] for p in poller.collected] == [10.0, 11.0, 12.0, 13.0, 14.0]
    assert len({p["timestamp"] for p in poller.collected}) == 5
    assert len(poller.latest_cpu_per_poll) >= 2
    assert set(poller.latest_cpu_per_poll) == {14.0}
