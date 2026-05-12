"""vLLM OpenAI-compatible URL and served model id for the scheduler gateway."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx


@dataclass(frozen=True)
class VllmRoute:
    """OpenAI-compatible root, e.g. ``http://127.0.0.1:8000/v1``."""

    api_base: str
    served_model_name: str


@dataclass
class VllmGatewayContext:
    """Shared HTTP client and route for gateway vLLM inference."""

    client: httpx.Client
    route: VllmRoute
    tokenizer_model_id: str


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def vllm_route_from_env() -> VllmRoute | None:
    """Return the configured route or ``None`` if ``VLLM_14B_BASE_URL`` is unset.

    Uses ``VLLM_14B_BASE_URL`` and optional ``VLLM_14B_MODEL`` (OpenAI ``model`` id).
    ``VLLM_8B_*`` env vars are ignored; one server handles all gateway traffic.
    """
    b14 = _env("VLLM_14B_BASE_URL")
    if not b14:
        return None
    m14 = _env("VLLM_14B_MODEL", "") or _default_served_name(
        b14,
        fallback="Qwen3-14B",
    )
    return VllmRoute(api_base=b14.rstrip("/"), served_model_name=m14)


def scheduler_vllm_openai_model_id() -> str | None:
    """OpenAI ``model`` id when ``VLLM_14B_BASE_URL`` is set; else ``None``.

    Financial and other **clients** may pass local paths (e.g. Qwen3-8B) in
    ``plain-completion`` JSON for bookkeeping; the vLLM gateway runs this id instead.
    """
    b14 = _env("VLLM_14B_BASE_URL")
    if not b14:
        return None
    return _env("VLLM_14B_MODEL", "") or _default_served_name(
        b14,
        fallback="Qwen3-14B",
    )


def scheduler_inference_log_model(
    *,
    gateway_origin: str | None = None,
    client_model: str | None = None,
    timeout_sec: float = 2.0,
) -> str:
    """Model id for stderr/logs (matches inference when possible).

    When the gateway process has ``VLLM_*`` set but this process does not (separate terminal),
    falls back to ``GET {origin}/health`` and reads ``vllm_model`` if ``backend`` is ``vllm``.
    Otherwise uses ``client_model`` (e.g. local path hint for label jobs).
    """
    env_id = scheduler_vllm_openai_model_id()
    if env_id:
        return env_id
    origin = (gateway_origin or "").strip().rstrip("/")
    if origin:
        try:
            import json
            from urllib.request import Request, urlopen

            req = Request(origin + "/health", method="GET")
            with urlopen(req, timeout=timeout_sec) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("backend") == "vllm" and isinstance(data.get("vllm_model"), str):
                m = data["vllm_model"].strip()
                if m:
                    return m
        except Exception:
            pass
    cm = (client_model or "").strip()
    return cm if cm else "gateway"


def _default_served_name(base_url: str, *, fallback: str) -> str:
    """Infer OpenAI ``model`` id from the URL path; otherwise ``fallback``.

    For ``http://127.0.0.1:8000/v1`` there is no path segment, so ``fallback`` is used
    (should match ``--served-model-name`` on that server).
    """
    raw = (base_url or "").strip()
    if not raw:
        return fallback
    p = urlparse(raw)
    path = (p.path or "").rstrip("/")
    if path.endswith("/v1"):
        path = path[: -len("/v1")].rstrip("/")
    if path:
        seg = path.split("/")[-1]
        if seg:
            return seg
    return fallback


def _vllm_probe_url_candidates(route: VllmRoute) -> list[str]:
    """Ordered URLs to check for a healthy OpenAI-style vLLM server."""
    base = route.api_base.rstrip("/")
    root = base.removesuffix("/v1") if base.endswith("/v1") else base
    candidates: list[str] = []
    if base.endswith("/v1"):
        candidates.append(f"{base}/models")
    candidates.append(urljoin(root + "/", "v1/models"))
    candidates.append(urljoin(root + "/", "health"))
    if not root.endswith("/"):
        candidates.append(f"{root}/health")

    seen: set[str] = set()
    ordered: list[str] = []
    for url in candidates:
        if url and url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered


def probe_vllm_route(client: httpx.Client, route: VllmRoute, timeout_sec: float = 3.0) -> bool:
    """True if the server looks like a reachable OpenAI-compatible vLLM instance.

    Tries several URLs: vLLM exposes ``GET /v1/models``; some builds also serve ``/health``.
    """
    for url in _vllm_probe_url_candidates(route):
        try:
            r = client.get(url, timeout=timeout_sec)
            if r.status_code < 500:
                return True
        except (OSError, httpx.RequestError):
            continue
    return False


def diagnose_vllm_metal_server(
    client: httpx.Client,
    route: VllmRoute,
    *,
    timeout_sec: float = 8.0,
) -> int:
    """Print a per-URL probe report; return 0 iff the server is healthy.

    Use this to confirm vLLM Metal (or any OpenAI-compatible server) is up before
    starting the gateway.
    """
    import json as json_lib
    import sys

    print(
        "vLLM Metal / OpenAI server check (scheduler gateway)",
        file=sys.stderr,
    )
    print(
        f"\napi_base={route.api_base!r} served_model={route.served_model_name!r}",
        file=sys.stderr,
    )
    lane_ok = False
    for url in _vllm_probe_url_candidates(route):
        try:
            r = client.get(url, timeout=timeout_sec)
            ct = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
            detail = f"status={r.status_code}"
            if ct == "application/json" and r.content:
                try:
                    data = r.json()
                    if isinstance(data, dict) and "data" in data:
                        ids = [
                            str(m.get("id", ""))
                            for m in data["data"][:4]
                            if isinstance(m, dict)
                        ]
                        detail += f" models={ids!r}"
                    else:
                        snippet = json_lib.dumps(data, ensure_ascii=False)[:180]
                        detail += f" body={snippet!r}"
                except (json_lib.JSONDecodeError, ValueError):
                    detail += f" body={(r.text or '')[:120]!r}"
            elif r.text:
                detail += f" body={(r.text or '')[:120]!r}"
            if r.status_code < 500:
                print(f"  GET {url} -> {detail}", file=sys.stderr)
                lane_ok = True
                break
            print(f"  GET {url} -> {detail} (trying next URL)", file=sys.stderr)
        except (OSError, httpx.RequestError) as exc:
            print(f"  GET {url} -> {type(exc).__name__}: {exc}", file=sys.stderr)
    if not lane_ok:
        print(
            "  Server not healthy — start vLLM, e.g.:\n"
            '    vllm serve "$HOME/models/Qwen3-14B" --port 8000 \\\n'
            "      --served-model-name Qwen3-14B",
            file=sys.stderr,
        )
        print("\nvLLM probe failed. Fix the server, then re-run.", file=sys.stderr)
        return 1
    print("\nvLLM responded (HTTP < 500 on at least one probe URL).", file=sys.stderr)
    return 0


def resolve_plain_completion_route(
    *,
    model_arg: str | None,
    scheduler_default_path: str,
    route: VllmRoute,
) -> tuple[VllmRoute, str]:
    """Return the single gateway route and canonical ``model`` string for logging.

    All plain-completion callers use the same vLLM server.
    """
    raw = (model_arg or "").strip() or scheduler_default_path
    return route, raw
