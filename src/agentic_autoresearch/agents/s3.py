"""RunPod S3-API helpers.

The RunPod "S3 API" is an S3-compatible facade over a network volume —
there is no separate object-storage product. Your bucket name is the
volume id. The pod writes images to /runpod-volume/runs/<id>/ as usual;
the S3 API lets the Mac (or any client) read them via presigned URLs
without copying anything locally.

Setup once (manual, in the RunPod console):
  Settings → S3 API Keys → Create. The created secret is DIFFERENT from
  the REST API key. Paste it into credentials.toml as:
    [runpod_s3]
    access_key = "<your RunPod user id>"
    secret_key = "<the new S3 API secret>"

Then the framework can:
  upload(local_path, key) — typically used FROM the pod, writing into
                            the same volume mounted as a filesystem.
                            (Equivalent to `cp local_path
                            /runpod-volume/<key>`.)
  presigned_url(key)     — generate a 24h URL the Mac (or browser) can
                            fetch directly from S3 without going through
                            our code or filesystem.
"""

from __future__ import annotations

from pathlib import Path


def endpoint_for(datacenter: str) -> str:
    """Map a RunPod DC id to its S3 endpoint."""
    return f"https://s3api-{datacenter.lower()}.runpod.io"


def make_client(creds, datacenter: str):
    """Construct a boto3 S3 client pointed at RunPod's S3 facade.

    `creds` is a Credentials dataclass; we need creds.runpod_s3 set.
    Lazy import of boto3 so this module is loadable without boto3
    installed (only the pod needs it).
    """
    if not hasattr(creds, "runpod_s3") or creds.runpod_s3 is None:
        raise RuntimeError(
            "no [runpod_s3] credentials configured. "
            "Console → Settings → S3 API Keys → Create, then add to "
            "~/.agentic-autoresearch/credentials.toml:\n"
            "  [runpod_s3]\n"
            "  access_key = \"<runpod user id>\"\n"
            "  secret_key = \"<s3 api secret>\""
        )
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=endpoint_for(datacenter),
        aws_access_key_id=creds.runpod_s3.access_key,
        aws_secret_access_key=creds.runpod_s3.secret_key,
        region_name=datacenter.lower(),
    )


def presigned_url(client, bucket: str, key: str, expires_in: int = 86400) -> str:
    """Generate a signed GET URL valid for `expires_in` seconds.

    The Mac stores the KEY in the DB, never the URL — URLs expire.
    Regenerate on every dashboard render.
    """
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires_in,
    )


def list_keys(client, bucket: str, prefix: str = "") -> list[dict]:
    """List object keys under a prefix."""
    out: list[dict] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for o in page.get("Contents", []) or []:
            out.append({"key": o["Key"], "size": o["Size"], "last_modified": str(o["LastModified"])})
    return out


def upload(client, bucket: str, local_path: Path, key: str) -> None:
    """Used by the pod to expose a file via the S3 API. Equivalent to
    writing to the volume directly — both surface as the same object."""
    client.upload_file(str(local_path), bucket, key)
