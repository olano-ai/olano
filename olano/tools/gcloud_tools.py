"""Google Cloud Platform tools — retired to an honest `[unsupported]`.

`gcloud` is not part of the sandbox image and its auth is a service-account
file, so it cannot run as a connected job (``core/job_runner.py``): the only
way to make it work would be a credential inside the agent's sandbox, which
Olano never provides (ADR-002). The tool names are kept so an ``agent.json5``
that still lists them loads without "unknown tool" — every call returns the
message below without shelling out.
"""

from __future__ import annotations

import logging
from typing import Annotated

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

_UNSUPPORTED = (
    "[unsupported] The gcloud CLI is not part of the supported toolkit: it would "
    "need a credential inside the agent's sandbox, which Olano never provides. "
    "Use the Google toolkit or an MCP server instead."
)


def _gcloud(args: list[str], timeout: int = 30) -> str:
    """Every gcloud tool lands here; nothing is ever executed."""
    del args, timeout
    return _UNSUPPORTED


@tool
def gcloud_project_info() -> str:
    """Get current GCP project info."""
    return (
        _gcloud(["config", "get-value", "project"])
        + "\n"
        + _gcloud(["projects", "describe", "$(gcloud config get-value project)"])
    )


@tool
def gcloud_compute_list(
    resource: Annotated[
        str, "Resource type: instances, disks, networks, firewalls, addresses"
    ] = "instances",
    zone: Annotated[str, "GCP zone (optional, e.g. us-central1-a)"] = "",
) -> str:
    """List GCP Compute Engine resources."""
    args = ["compute", resource, "list"]
    if zone:
        args += ["--zones", zone]
    return _gcloud(args)


@tool
def gcloud_storage_list(
    bucket: Annotated[str, "GCS bucket name (without gs://). Empty = list all buckets."] = "",
    prefix: Annotated[str, "Object prefix filter"] = "",
) -> str:
    """List GCS buckets or objects within a bucket."""
    if bucket:
        path = f"gs://{bucket}/{prefix}"
        return _gcloud(["storage", "ls", path])
    return _gcloud(["storage", "buckets", "list"])


@tool
def gcloud_storage_upload(
    local_path: Annotated[str, "Local file path to upload"],
    bucket: Annotated[str, "GCS bucket name (without gs://)"],
    object_name: Annotated[str, "Object name in bucket (optional)"] = "",
) -> str:
    """Upload a file to Google Cloud Storage."""
    dest = f"gs://{bucket}/{object_name or local_path.split('/')[-1]}"
    return _gcloud(["storage", "cp", local_path, dest], timeout=60)


@tool
def gcloud_storage_download(
    bucket: Annotated[str, "GCS bucket name (without gs://)"],
    object_name: Annotated[str, "Object name in bucket"],
    local_path: Annotated[str, "Local destination path"],
) -> str:
    """Download a file from Google Cloud Storage."""
    src = f"gs://{bucket}/{object_name}"
    return _gcloud(["storage", "cp", src, local_path], timeout=60)


@tool
def gcloud_logs_read(
    filter_str: Annotated[
        str, "Log filter (e.g. 'severity>=ERROR' or 'resource.type=gce_instance')"
    ] = "",
    limit: Annotated[int, "Max log entries to return"] = 20,
    project: Annotated[str, "GCP project ID (optional)"] = "",
) -> str:
    """Read GCP Cloud Logging entries."""
    args = ["logging", "read"]
    if filter_str:
        args.append(filter_str)
    args += ["--limit", str(limit)]
    if project:
        args += ["--project", project]
    return _gcloud(args)


@tool
def gcloud_run_list(
    region: Annotated[str, "GCP region (optional, e.g. us-central1)"] = "",
) -> str:
    """List Cloud Run services."""
    args = ["run", "services", "list"]
    if region:
        args += ["--region", region]
    return _gcloud(args)


@tool
def gcloud_deploy(
    service_name: Annotated[str, "Cloud Run service name"],
    image: Annotated[str, "Container image URL (e.g. gcr.io/project/image:tag)"],
    region: Annotated[str, "GCP region"] = "us-central1",
    memory: Annotated[str, "Memory limit (e.g. 512Mi, 1Gi)"] = "512Mi",
    allow_unauthenticated: Annotated[bool, "Allow unauthenticated access"] = False,
) -> str:
    """Deploy a container to Cloud Run."""
    args = [
        "run",
        "deploy",
        service_name,
        "--image",
        image,
        "--region",
        region,
        "--memory",
        memory,
    ]
    if allow_unauthenticated:
        args.append("--allow-unauthenticated")
    else:
        args.append("--no-allow-unauthenticated")
    return _gcloud(args, timeout=120)


@tool
def gcloud_run_command(
    command: Annotated[
        str, "Full gcloud command without the 'gcloud' prefix (e.g. 'compute instances list')"
    ],
) -> str:
    """Run an arbitrary read-only gcloud command. Destructive operations are blocked."""
    return _gcloud(command.split())


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
