# RunPod operational notes (2026-05-22)

Verified against docs.runpod.io and live experiments tonight. Update when
upstream changes.

## API surface
- REST primary at `https://rest.runpod.io/v1/` (Bearer auth).
- GraphQL still exists at `api.runpod.io/graphql` (browser UA needed) but
  trails REST on new fields. Don't depend on `runtime.ports` from it.
- Datacenter codes: two prefixes in EU — `EU-RO-1` AND `EUR-IS-1` /
  `EUR-NO-1`. Match the volume's exact DC; mismatches → opaque 404.

## POST /v1/pods
Required: `imageName`. Everything else has defaults but for the loop we set:

```
imageName            runpod/base:0.6.1-cuda12.1.0  (or pytorch image)
gpuTypeIds           ["NVIDIA A40", ...]    PLURAL, array of full names
gpuCount             1
containerDiskInGb    50   (only if not pulling big stuff; HF caches → volume)
volumeMountPath      "/workspace"   (canonical) — we use /runpod-volume
                      historically, both work as long as consistent
networkVolumeId      "..."
dataCenterIds        ["EU-RO-1"]   PLURAL
ports                ["22/tcp", "8188/http"]   array of "<port>/<proto>"
cloudType            "SECURE"   REQUIRED for network-volume pods.
                      Community cloud silently ignores volumes.
env                  {}
supportPublicIp      true
interruptible        false
```

## "Really ready" gate
`desiredStatus=RUNNING` alone is a LIE — pod can be in scheduling queue.
Poll for ALL THREE:

```
desiredStatus == "RUNNING"
publicIp != ""
portMappings["22"] is set  (REST schema: GET /v1/pods/{id} returns
                            portMappings as {"22": 12345, ...})
```

Then TCP-probe the mapped port (it can take ~5s after the API reports
ready before sshd accepts).

## SSH
- `<podId>@ssh.runpod.io` — TCP proxy, **interactive shell only**. No
  scp/sftp. Don't use this for the framework's actor.
- Direct: `root@<publicIp> -p <portMappings["22"]>` — full SSH; supports
  scp, sftp, rsync. **Use this.**
- Your pubkey MUST be on the account (Settings → SSH keys, or
  `runpodctl ssh add-key`). RunPod injects it as `PUBLIC_KEY` env at boot.

## Storage tiers
| tier            | persists stop? | on terminate? | cost                         |
| --------------- | -------------- | ------------- | ---------------------------- |
| container disk  | no             | gone          | $0.10/GB-mo while running     |
| volume disk     | yes            | gone          | $0.10 running / $0.20 stopped|
| network volume  | yes            | SURVIVES      | $0.07/GB-mo always            |

Defaults to put on volume (so container disk doesn't fill at 30GB):

```
HF_HOME=/runpod-volume/.hf_cache
TRANSFORMERS_CACHE=/runpod-volume/.hf_cache
TORCH_HOME=/runpod-volume/.torch
XDG_CACHE_HOME=/runpod-volume/.cache
```

ComfyUI models live at `/runpod-volume/ComfyUI/models/...` (mirrors the
canonical layout).

## Network volume concurrency
Writes from multiple pods at once = corruption risk. Reads concurrent OK.
Our design only writes from one iter's pod at a time → safe.

Backed by MooseFS (`mfs#euro.runpod.net:9421`) — decent sequential IO,
slow on many small files. `aws s3 sync` against the S3 API breaks past
~10k objects; use direct `cp --recursive` or chunk uploads.

## S3-compatible API
The network volume IS the bucket. There is NO separate object-storage
product; the S3 API just speaks S3 protocol on top of the volume.

```
Endpoint:    https://s3api-<datacenter-lowercase>.runpod.io/
            e.g. https://s3api-eu-ro-1.runpod.io
Access key:  your RunPod USER_ID (not the REST API key)
Secret key:  a SEPARATE "S3 API key" created in
             RunPod console → Settings → S3 API Keys
Bucket name: the network volume ID (e.g. "8xoug20653")
Region:      use the datacenter id lowercased
            (boto3 quirk; some endpoints accept "" too)
```

Working boto3 pattern:

```python
import boto3
s3 = boto3.client(
    "s3",
    endpoint_url="https://s3api-eu-ro-1.runpod.io",
    aws_access_key_id="<user_id>",
    aws_secret_access_key="<s3_secret>",
    region_name="eu-ro-1",
)
# write
s3.upload_file("/local/file.webp", "8xoug20653", "runs/iter_007/winner.webp")
# read URL
url = s3.generate_presigned_url(
    "get_object",
    Params={"Bucket": "8xoug20653", "Key": "runs/iter_007/winner.webp"},
    ExpiresIn=86400,
)
```

Egress is currently free (not contractually guaranteed; could change).

## Exposed HTTP ports
`"8188/http"` auto-creates `https://<podId>-8188.proxy.runpod.net`.
Requirements:
- service binds `0.0.0.0`, NOT `127.0.0.1`
- the pod_id, not the user, is the only "auth" on these URLs
- Cloudflare 100-second idle timeout — long ComfyUI generations may 524.
  Use TCP + direct IP for long jobs, or chunk responses.
- Max 10 HTTP ports per pod.

## Billing
- Per-second. Billed from RUNNING, **before container is fully booted**.
- Pre-baked images save the pull window.
- Stopped pod: compute stops, volume disk doubles to $0.20/GB-mo,
  container disk freed, network volume billed continuously regardless.
- Terminate: both disks deleted, only network volume persists.

## Gotchas observed tonight
- Pod with `desiredStatus=RUNNING` and blank `publicIp` was actually
  still scheduling. Wait for the IP, don't trust the status alone.
- Civitai downloads from the install script 401 without a Civitai
  account token. Skip Civitai-dependent stacks (SDXL/RealVisXL) or
  add a Civitai key to credentials.toml. Flux/Qwen/PuLID are all on
  HuggingFace, no auth gates beyond your HF token.
- `python3-pip` is not apt-installable on `runpod/base:0.6.1`. Use
  `python3 -m pip` directly (it's already installed via ensurepip).
- HuggingFace's `HF_HUB_ENABLE_HF_TRANSFER` env is deprecated in 2026
  for `HF_XET_HIGH_PERFORMANCE` (warning only, still works).
