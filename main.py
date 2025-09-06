import os
import time
import uuid
import yaml
import asyncio
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request
from pydantic import BaseModel
from kubernetes import client, config
import threading
from typing import Dict, Optional
from kubernetes.config.config_exception import ConfigException

# --- Load Kubernetes configuration ---
# When running in-cluster on GKE, this will auto-configure. Fallback to local kubeconfig for dev.
try:
    config.load_incluster_config()
except ConfigException:
    config.load_kube_config()

# Kubernetes API clients
apps_v1 = client.AppsV1Api()
core_v1 = client.CoreV1Api()
networking_v1 = client.NetworkingV1Api()

# Image tag for the session-bubble image used in per-session pods
# Allow override via env var; default to 'latest'
SESSION_BUBBLE_IMAGE_TAG = os.environ.get("SESSION_BUBBLE_IMAGE_TAG", "latest")

app = FastAPI()


class SessionResponse(BaseModel):
    sessionId: str
    commandUrl: str
    streamUrl: str


# --- Job-based session creation data models and storage ---
job_status: Dict[str, dict] = {}
job_payloads: Dict[str, dict] = {}


class SessionStartResponse(BaseModel):
    jobId: str


class StartSessionRequest(BaseModel):
    livekit_room_name: str | None = None
    livekit_api_key: str | None = None
    livekit_api_secret: str | None = None
    livekit_url: str | None = None


class SessionStatusResponse(BaseModel):
    status: str  # "PENDING" | "READY" | "FAILED"
    sessionId: Optional[str] = None
    commandUrl: Optional[str] = None
    streamUrl: Optional[str] = None
    error: Optional[str] = None


def render_template(template_name: str, session_id: str) -> dict:
    """Load a YAML template, replace placeholders, and return as a dict."""
    with open(f"templates/{template_name}", "r") as f:
        template_str = f.read()

    rendered_str = template_str.replace("{{SESSION_ID}}", session_id)
    rendered_str = rendered_str.replace("{{IMAGE_TAG}}", SESSION_BUBBLE_IMAGE_TAG)
    return yaml.safe_load(rendered_str)


def _wait_for_deployment_ready(deployment_name: str, namespace: str, timeout_seconds: int = 180) -> bool:
    """Poll the Deployment status until at least one replica is ready or timeout."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            dep = apps_v1.read_namespaced_deployment_status(name=deployment_name, namespace=namespace)
            status = dep.status
            ready = (getattr(status, "ready_replicas", 0) or 0) >= 1
            available = (getattr(status, "available_replicas", 0) or 0) >= 1
            if ready and available:
                return True
        except client.ApiException as e:
            # If not found yet, give it a moment
            if e.status != 404:
                raise
        time.sleep(2)
    return False


# No per-session Service; LiveKit connections are outbound only.


def _cleanup_k8s_resources(session_id: str, namespace: str) -> None:
    """Best-effort cleanup of K8s resources for a given session (sync)."""
    # Delete Deployment
    try:
        apps_v1.delete_namespaced_deployment(name=f"session-{session_id}", namespace=namespace)
    except client.ApiException as e:
        if e.status != 404:
            print(f"Error deleting deployment: {e.reason}")


def _create_session_worker(job_id: str) -> None:
    """Background worker to create K8s resources and wait for readiness, updating job_status."""
    session_id = f"sess-{uuid.uuid4().hex[:8]}"
    namespace = os.environ.get("K8S_NAMESPACE", "default")

    job_status[job_id] = {"status": "PENDING"}

    try:
        # Create Deployment
        deployment_body = render_template("deployment-template.yaml", session_id)

        # Inject runtime overrides from payload (LIVEKIT_ROOM_NAME and optional credential overrides)
        payload = job_payloads.get(job_id) or {}
        try:
            containers = deployment_body["spec"]["template"]["spec"]["containers"]
            if containers:
                env_list = containers[0].get("env", [])
                # Ensure LIVEKIT_ROOM_NAME is set to provided room name, else default to session_id
                room_env = next((e for e in env_list if e.get("name") == "LIVEKIT_ROOM_NAME"), None)
                room_value = payload.get("livekit_room_name") or session_id
                if room_env:
                    room_env["value"] = room_value
                    room_env.pop("valueFrom", None)
                else:
                    env_list.append({"name": "LIVEKIT_ROOM_NAME", "value": room_value})

                # Optional: allow overriding credentials and URL per request
                for key, payload_key in (
                    ("LIVEKIT_URL", "livekit_url"),
                    ("LIVEKIT_API_KEY", "livekit_api_key"),
                    ("LIVEKIT_API_SECRET", "livekit_api_secret"),
                ):
                    override = payload.get(payload_key)
                    if override:
                        # Find existing env and convert to literal value
                        env_item = next((e for e in env_list if e.get("name") == key), None)
                        if env_item:
                            env_item["value"] = override
                            env_item.pop("valueFrom", None)
                        else:
                            env_list.append({"name": key, "value": override})
                containers[0]["env"] = env_list
        except Exception as e:
            print(f"Warning: failed to apply payload env overrides: {e}")
        apps_v1.create_namespaced_deployment(namespace=namespace, body=deployment_body)

        # Wait for readiness
        deployment_name = f"session-{session_id}"
        dep_ready = _wait_for_deployment_ready(deployment_name, namespace, timeout_seconds=180)

        if not dep_ready:
            _cleanup_k8s_resources(session_id, namespace)
            job_status[job_id] = {
                "status": "FAILED",
                "error": "Session Deployment did not become ready in time. Please try again.",
            }
            return

        # Success: return only session info; token generation is handled upstream by token service
        job_status[job_id] = {
            "status": "READY",
            "sessionId": session_id,
        }
        return

    except Exception as e:
        # Failure path
        try:
            _cleanup_k8s_resources(session_id, namespace)
        except Exception:
            pass
        job_status[job_id] = {"status": "FAILED", "error": str(e)}


@app.get("/health", status_code=200)
async def health_check():
    """A simple health check endpoint."""
    return {"status": "ok"}


@app.post("/sessions", response_model=SessionStartResponse, status_code=202)
@app.post("/api/sessions", response_model=SessionStartResponse, status_code=202)
async def start_session_creation(req: StartSessionRequest | None = None, request: Request = None):
    """Start a background job to create a session and return a job ID immediately."""
    # Optional internal auth
    expected_secret = os.environ.get("INTERNAL_SECRET_KEY")
    if expected_secret:
        provided_secret = request.headers.get("X-Internal-Secret") if request else None
        if provided_secret != expected_secret:
            raise HTTPException(status_code=401, detail="Unauthorized")
    job_id = f"job-{uuid.uuid4().hex[:8]}"
    # Store payload for the worker
    job_payloads[job_id] = (req.dict() if req else {})
    thread = threading.Thread(target=_create_session_worker, args=(job_id,), daemon=True)
    thread.start()
    return SessionStartResponse(jobId=job_id)


@app.get("/sessions/status/{job_id}", response_model=SessionStatusResponse)
@app.get("/api/sessions/status/{job_id}", response_model=SessionStatusResponse)
async def get_session_status(job_id: str):
    status = job_status.get(job_id)
    if not status:
        raise HTTPException(status_code=404, detail="Job not found.")
    return status


@app.delete("/sessions/{session_id}", status_code=204)
@app.delete("/api/sessions/{session_id}", status_code=204)
async def delete_session(session_id: str):
    """Delete all K8s resources for a given session."""
    namespace = os.environ.get("K8S_NAMESPACE", "default")
    _cleanup_k8s_resources(session_id, namespace)
    return {}


# Legacy VNC/WebSocket proxy endpoints removed (LiveKit-only architecture)
