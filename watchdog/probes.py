"""The two health signals the watchdog reads each poll, and how they combine.

Why two signals, not one:

* **HTTP ``GET /health``** answers *"can the service actually do its job right
  now?"* — it returns 200 only if the app can round-trip both Postgres and Redis.
  This is a *readiness* check. It can fail while the process is perfectly alive
  (a dependency is down, the app is wedged on a lock).
* **Container state over the Docker API** answers *"does the orchestrator still
  have this thing running?"* — `running` / `exited` / `restarting` / `dead`, plus
  Docker's own healthcheck verdict. This is closer to a *liveness* check, seen
  from outside the container.

They fail in different situations and the difference is diagnostic:

| container | /health | most likely meaning                         |
|-----------|---------|---------------------------------------------|
| running   | 200     | healthy — nothing to do                      |
| exited    | refused | process crashed / OOM-killed → restart helps |
| running   | 503     | app up, a dependency (PG/Redis) unreachable  |
| running   | timeout | app process alive but stuck → restart helps  |

The watchdog treats the target as failing if *either* signal is bad, and records
*which* one in the incident row (``detected_via`` = ``container`` or ``health``),
so the Slack message and the DB say something useful about the fault.

Note on the ``running`` + 503 row: a feed-service restart will not fix a downed
Postgres. We still open an incident and alert (a human needs to know), and we
still issue the restart — it is cheap, it clears any half-open connection pool,
and if the dependency has already recovered it gets the app writing again
immediately instead of waiting on the app's own backoff. The value of the
watchdog in that case is the *alert*, not the restart. This is called out again
where the restart happens in ``__main__``.

Only ``docker`` (docker-py) is a third-party import. The HTTP poll uses
``urllib`` from the standard library — one less dependency to install and patch
on the host, and a plain ``urlopen`` is trivial to explain line by line.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

import docker
from docker.errors import DockerException, NotFound

# Container states Docker can report. Only "running" is acceptable; everything
# else (including "restarting" and "created") means the service is not up.
_HEALTHY_CONTAINER_STATE = "running"


@dataclass(frozen=True)
class Verdict:
    """One poll's combined result.

    ``ok`` is the only field the state machine looks at. The rest describe the
    fault for the incident row / Slack alert.
    """

    ok: bool
    detected_via: str      # "health" | "container" | "watchdog" (probe error) — empty when ok
    symptom: str           # human-readable; empty when ok
    # Raw sub-results, useful for logging every poll at DEBUG.
    container_state: Optional[str] = None
    health_status: Optional[int] = None  # HTTP status code, or None if the request never completed

    @classmethod
    def healthy(cls, *, container_state: str, health_status: int) -> "Verdict":
        return cls(
            ok=True,
            detected_via="",
            symptom="",
            container_state=container_state,
            health_status=health_status,
        )


class HealthProbe:
    """HTTP ``GET /health`` with a hard timeout.

    The timeout is also the ceiling on how long a wedged app can make one poll
    hang — detection is timeout-based, there is no signal from the app saying
    "I'm stuck". A request that times out or refuses counts as a failure just
    like an explicit 503.
    """

    def __init__(self, url: str, timeout_seconds: float) -> None:
        self._url = url
        self._timeout = timeout_seconds

    def check(self) -> tuple[bool, str, Optional[int]]:
        """Return ``(ok, symptom, http_status)``."""
        try:
            req = urllib.request.Request(self._url, method="GET")
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                status = resp.getcode()
                body = resp.read(4096).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            # 4xx/5xx still give us a response body — /health puts the failing
            # dependency name in it, so surface that in the symptom.
            body = ""
            try:
                body = exc.read(4096).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            reason = _reason_from_body(body) or f"HTTP {exc.code}"
            return False, f"/health returned {exc.code}: {reason}", exc.code
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            # Connection refused (container down), DNS, or the timeout fired.
            return False, f"/health unreachable: {exc}", None

        if status != 200:
            reason = _reason_from_body(body) or f"HTTP {status}"
            return False, f"/health returned {status}: {reason}", status
        return True, "", status


class ContainerProbe:
    """Reads the feed-service container's state through the Docker API.

    Uses ``docker.from_env()``, so it honours ``DOCKER_HOST`` when set (needed
    with Colima locally) and falls back to the default ``/var/run/docker.sock``
    (the case on the EC2 box). The watchdog runs on the host and is bind-mounted
    the socket exactly like an L1 tool that observes containers from outside.
    """

    def __init__(self, container_name: str) -> None:
        self.name = container_name
        # One client for the life of the process; docker-py manages the
        # connection pool. Constructed here so a broken Docker socket fails at
        # startup, not mid-incident.
        self._client = docker.from_env()

    def check(self) -> tuple[bool, str, Optional[str]]:
        """Return ``(ok, symptom, container_state)``."""
        try:
            container = self._client.containers.get(self.name)
        except NotFound:
            return False, f"container '{self.name}' does not exist", None
        except DockerException as exc:
            # Docker daemon unreachable. That's a watchdog-host problem, not a
            # feed-service problem — report it as such rather than restarting.
            return False, f"cannot query Docker for '{self.name}': {exc}", None

        state = container.status  # 'running', 'exited', 'restarting', 'dead', ...
        if state != _HEALTHY_CONTAINER_STATE:
            exit_code = container.attrs.get("State", {}).get("ExitCode")
            suffix = f" (exit code {exit_code})" if exit_code not in (None, 0) else ""
            return False, f"container state is '{state}'{suffix}", state

        # If the container declares a Docker HEALTHCHECK, respect an explicit
        # "unhealthy". "starting" is not a failure — it just hasn't passed yet.
        docker_health = (
            container.attrs.get("State", {}).get("Health", {}).get("Status")
        )
        if docker_health == "unhealthy":
            return False, "container reports docker health = unhealthy", state

        return True, "", state

    def restart(self, timeout_seconds: int = 10) -> None:
        """Restart the container (SIGTERM, then SIGKILL after ``timeout_seconds``)."""
        self._client.containers.get(self.name).restart(timeout=timeout_seconds)


def _reason_from_body(body: str) -> str:
    """Pull the failing-dependency reason out of a ``/health`` JSON body.

    feed-service returns e.g. ``{"status":"unhealthy","postgres":"ok",
    "redis":"error: ..."}``. Return a compact "redis: error: ..." string, or ""
    if the body isn't the shape we expect.
    """
    try:
        obj = json.loads(body)
    except (ValueError, TypeError):
        return ""
    if not isinstance(obj, dict):
        return ""
    bad = [
        f"{k}: {v}"
        for k, v in obj.items()
        if k in ("postgres", "redis") and isinstance(v, str) and v != "ok"
    ]
    return "; ".join(bad)


class Prober:
    """Runs both sub-probes each poll and folds them into one :class:`Verdict`.

    Order matters: check the container first. If the container isn't running, the
    HTTP poll is guaranteed to fail too, and "container is exited" is the more
    precise symptom than "/health unreachable". Only fall through to the HTTP
    verdict when the container looks fine.
    """

    def __init__(self, health: HealthProbe, container: ContainerProbe) -> None:
        self._health = health
        # Public: the poll loop reuses this same object to issue the restart, so
        # there is one Docker client, not two.
        self.container = container

    def poll(self) -> Verdict:
        c_ok, c_symptom, c_state = self.container.check()
        if not c_ok:
            return Verdict(
                ok=False,
                detected_via="container",
                symptom=c_symptom,
                container_state=c_state,
            )

        h_ok, h_symptom, h_status = self._health.check()
        if not h_ok:
            return Verdict(
                ok=False,
                detected_via="health",
                symptom=h_symptom,
                container_state=c_state,
                health_status=h_status,
            )

        return Verdict.healthy(container_state=c_state, health_status=h_status)
