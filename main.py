import os
import time
import uuid
import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from kubernetes import client, config
import threading
from typing import Dict, Optional
from kubernetes.config.config_exception import ConfigException
import re

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

# Image tag for the session bubble image
# The manager's job is to always deploy the latest stable VNC image.
VNC_IMAGE_TAG = "latest"

app = FastAPI()


class SessionResponse(BaseModel):
    sessionId: str
    commandUrl: str
    streamUrl: str


# --- Job-based session creation data models and storage ---
job_status: Dict[str, dict] = {}


class SessionStartResponse(BaseModel):
    jobId: str


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
    rendered_str = rendered_str.replace("{{IMAGE_TAG}}", VNC_IMAGE_TAG)
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


def _endpoints_has_addresses(ep) -> bool:
    """Return True if the Endpoints object has at least one ready address in any subset."""
    if not ep or not ep.subsets:
        return False
    for subset in ep.subsets:
        addrs = getattr(subset, "addresses", None)
        if addrs and len(addrs) > 0:
            return True
    return False


def _wait_for_service_endpoints(service_name: str, namespace: str, timeout_seconds: int = 120) -> bool:
    """Poll the Service Endpoints until at least one address is ready or timeout."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            ep = core_v1.read_namespaced_endpoints(name=service_name, namespace=namespace)
            if _endpoints_has_addresses(ep):
                return True
        except client.ApiException as e:
            if e.status != 404:
                raise
        time.sleep(2)
    return False


def _cleanup_k8s_resources(session_id: str, namespace: str) -> None:
    """Best-effort cleanup of K8s resources for a given session (sync)."""
    # Delete Deployment
    try:
        apps_v1.delete_namespaced_deployment(name=f"session-{session_id}", namespace=namespace)
    except client.ApiException as e:
        if e.status != 404:
            print(f"Error deleting deployment: {e.reason}")

    # Delete Service
    try:
        core_v1.delete_namespaced_service(name=f"service-{session_id}", namespace=namespace)
    except client.ApiException as e:
        if e.status != 404:
            print(f"Error deleting service: {e.reason}")

    # Delete Ingress
    try:
        networking_v1.delete_namespaced_ingress(name=f"ingress-{session_id}", namespace=namespace)
    except client.ApiException as e:
        if e.status != 404:
            print(f"Error deleting ingress: {e.reason}")


def _add_session_to_main_ingress(session_id: str, namespace: str) -> None:
    """Add host rules and TLS host for the session to the shared main Ingress.

    Routes traffic for:
      - /command -> service-{session_id}: port name "command-ws"
      - /stream  -> service-{session_id}: port name "vnc-stream"

    Ensures TLS includes the hostname with a placeholder secret to satisfy
    the GKE Ingress controller, while TLS termination uses the pre-shared cert.
    """
    host = f"{session_id}.vnc.shodh.ai"
    ingress_name = "session-bubble-ingress"

    ing: client.V1Ingress = networking_v1.read_namespaced_ingress(
        name=ingress_name, namespace=namespace
    )

    if ing.spec is None:
        ing.spec = client.V1IngressSpec()

    # Ensure rules exists and the host rule is present
    rules = list(ing.spec.rules or [])
    exists = False
    for r in rules:
        if r.host == host:
            exists = True
            break
    if not exists:
        paths = [
            client.V1HTTPIngressPath(
                path="/stream",
                path_type="Prefix",
                backend=client.V1IngressBackend(
                    service=client.V1IngressServiceBackend(
                        name=f"service-{session_id}",
                        port=client.V1ServiceBackendPort(name="vnc-stream"),
                    )
                ),
            ),
            client.V1HTTPIngressPath(
                path="/command",
                path_type="Prefix",
                backend=client.V1IngressBackend(
                    service=client.V1IngressServiceBackend(
                        name=f"service-{session_id}",
                        port=client.V1ServiceBackendPort(name="command-ws"),
                    )
                ),
            ),
        ]
        rule = client.V1IngressRule(host=host, http=client.V1HTTPIngressRuleValue(paths=paths))
        rules.append(rule)
        ing.spec.rules = rules

    # Ensure TLS block includes the host and has a secretName
    tls_list = list(ing.spec.tls or [])
    if not tls_list:
        tls_list = [client.V1IngressTLS(hosts=[host], secret_name="ingress-tls-placeholder")]
    else:
        tls0 = tls_list[0]
        tls0.hosts = list(tls0.hosts or [])
        if host not in tls0.hosts:
            tls0.hosts.append(host)
        if not tls0.secret_name:
            tls0.secret_name = "ingress-tls-placeholder"
        tls_list[0] = tls0
    ing.spec.tls = tls_list

    networking_v1.patch_namespaced_ingress(name=ingress_name, namespace=namespace, body=ing)


def _remove_session_from_main_ingress(session_id: str, namespace: str) -> None:
    """Remove host rules and TLS host for the session from the shared main Ingress."""
    host = f"{session_id}.vnc.shodh.ai"
    ingress_name = "session-bubble-ingress"

    try:
        ing: client.V1Ingress = networking_v1.read_namespaced_ingress(name=ingress_name, namespace=namespace)
    except client.ApiException as e:
        if e.status == 404:
            return
        raise

    changed = False
    if ing.spec and ing.spec.rules:
        new_rules = [r for r in ing.spec.rules if r.host != host]
        if len(new_rules) != len(ing.spec.rules):
            ing.spec.rules = new_rules
            changed = True

    if ing.spec and ing.spec.tls:
        tls0 = ing.spec.tls[0]
        if tls0.hosts and host in tls0.hosts:
            tls0.hosts = [h for h in tls0.hosts if h != host]
            changed = True

    if changed:
        networking_v1.patch_namespaced_ingress(name=ingress_name, namespace=namespace, body=ing)


def _reconcile_ingress_sessions(namespace: str = "default", interval_seconds: int = 30) -> None:
    """Background loop that prunes stale session hosts from the shared Ingress.

    Any host like sess-XXXXXXXX.vnc.shodh.ai whose corresponding Service
    (service-sess-XXXXXXXX) no longer exists will be removed from the Ingress
    rules and TLS hosts. This prevents the GCLB controller from failing
    translation due to dead backends, and eliminates the need for manual url-map
    cleanups when sessions are deleted out-of-band.
    """
    host_re = re.compile(r"^sess-[a-f0-9]{8}\.vnc\.shodh\.ai$")
    ingress_name = "session-bubble-ingress"
    while True:
        try:
            ing: client.V1Ingress = networking_v1.read_namespaced_ingress(
                name=ingress_name, namespace=namespace
            )
            changed = False

            # Build a set of live session service names for quick checks
            rules = list(ing.spec.rules or [])
            to_keep = []
            stale_hosts = set()
            for r in rules:
                h = getattr(r, "host", "") or ""
                if host_re.match(h):
                    sess_id = h.split(".")[0]  # e.g., sess-xxxxxxxx
                    svc_name = f"service-{sess_id}"
                    try:
                        core_v1.read_namespaced_service(name=svc_name, namespace=namespace)
                        to_keep.append(r)
                    except client.ApiException as e:
                        if e.status == 404:
                            stale_hosts.add(h)
                            changed = True
                        else:
                            # Non-404 errors: keep rule, try next round
                            to_keep.append(r)
                    except Exception:
                        to_keep.append(r)
                else:
                    to_keep.append(r)

            if changed:
                ing.spec.rules = to_keep
                # Clean TLS hosts for removed hosts
                if ing.spec.tls:
                    tls0 = ing.spec.tls[0]
                    cur_hosts = list(tls0.hosts or [])
                    new_hosts = [h for h in cur_hosts if h not in stale_hosts]
                    tls0.hosts = new_hosts
                    ing.spec.tls[0] = tls0
                networking_v1.patch_namespaced_ingress(name=ingress_name, namespace=namespace, body=ing)
        except Exception:
            # Best-effort reconciler; swallow errors and retry
            pass
        time.sleep(interval_seconds)


def _create_session_worker(job_id: str) -> None:
    """Background worker to create K8s resources and wait for readiness, updating job_status."""
    session_id = f"sess-{uuid.uuid4().hex[:8]}"
    namespace = os.environ.get("K8S_NAMESPACE", "default")

    job_status[job_id] = {"status": "PENDING"}

    try:
        # Create Deployment
        deployment_body = render_template("deployment-template.yaml", session_id)
        apps_v1.create_namespaced_deployment(namespace=namespace, body=deployment_body)

        # Create Service
        service_body = render_template("service-template.yaml", session_id)
        core_v1.create_namespaced_service(namespace=namespace, body=service_body)

        # Route traffic for this session via the shared main Ingress
        _add_session_to_main_ingress(session_id, namespace)

        # Wait for readiness
        deployment_name = f"session-{session_id}"
        service_name = f"service-{session_id}"
        dep_ready = _wait_for_deployment_ready(deployment_name, namespace, timeout_seconds=180)
        svc_ready = _wait_for_service_endpoints(service_name, namespace, timeout_seconds=120)

        if not dep_ready or not svc_ready:
            _cleanup_k8s_resources(session_id, namespace)
            job_status[job_id] = {
                "status": "FAILED",
                "error": "Session environment did not become ready in time. Please try again.",
            }
            return

        # Success
        job_status[job_id] = {
            "status": "READY",
            "sessionId": session_id,
            "commandUrl": f"wss://{session_id}.vnc.shodh.ai/command",
            "streamUrl": f"wss://{session_id}.vnc.shodh.ai/stream",
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
async def start_session_creation():
    """Start a background job to create a session and return a job ID immediately."""
    job_id = f"job-{uuid.uuid4().hex[:8]}"
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
    # Remove routing from the shared main Ingress first (best effort)
    try:
        _remove_session_from_main_ingress(session_id, namespace)
    except Exception:
        pass
    _cleanup_k8s_resources(session_id, namespace)
    return {}


# Start background reconciler to keep Ingress clean of stale session hosts
_reconciler_thread = threading.Thread(
    target=_reconcile_ingress_sessions,
    kwargs={"namespace": os.environ.get("K8S_NAMESPACE", "default"), "interval_seconds": 30},
    daemon=True,
)
_reconciler_thread.start()
