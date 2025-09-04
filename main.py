import os
import uuid
import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from kubernetes import client, config
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

# Image tag for the session bubble image
VNC_IMAGE_TAG = os.environ.get("VNC_IMAGE_TAG", "latest")

app = FastAPI()


class SessionResponse(BaseModel):
    sessionId: str
    commandUrl: str
    streamUrl: str


def render_template(template_name: str, session_id: str) -> dict:
    """Load a YAML template, replace placeholders, and return as a dict."""
    with open(f"templates/{template_name}", "r") as f:
        template_str = f.read()

    rendered_str = template_str.replace("{{SESSION_ID}}", session_id)
    rendered_str = rendered_str.replace("{{IMAGE_TAG}}", VNC_IMAGE_TAG)
    return yaml.safe_load(rendered_str)


@app.get("/health", status_code=200)
async def health_check():
    """A simple health check endpoint."""
    return {"status": "ok"}


@app.post("/sessions", response_model=SessionResponse)
async def create_session():
    """Create a new isolated session by provisioning Deployment, Service, and Ingress."""
    session_id = f"sess-{uuid.uuid4().hex[:8]}"
    namespace = os.environ.get("K8S_NAMESPACE", "default")

    try:
        # 1) Deployment
        deployment_body = render_template("deployment-template.yaml", session_id)
        apps_v1.create_namespaced_deployment(namespace=namespace, body=deployment_body)

        # 2) Service
        service_body = render_template("service-template.yaml", session_id)
        core_v1.create_namespaced_service(namespace=namespace, body=service_body)

        # 3) Ingress
        ingress_body = render_template("ingress-template.yaml", session_id)
        networking_v1.create_namespaced_ingress(namespace=namespace, body=ingress_body)

    except client.ApiException as e:
        # Best-effort cleanup
        await delete_session(session_id)
        raise HTTPException(status_code=500, detail=f"Kubernetes API error: {e.reason}")

    # Return endpoints
    return SessionResponse(
        sessionId=session_id,
        commandUrl=f"wss://vnc.shodh.ai/{session_id}/command",
        streamUrl=f"wss://vnc.shodh.ai/{session_id}/stream",
    )


@app.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: str):
    """Delete all K8s resources for a given session."""
    namespace = os.environ.get("K8S_NAMESPACE", "default")

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

    return {}
