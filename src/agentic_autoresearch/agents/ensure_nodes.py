"""ensure_nodes — pod-side custom-node installer.

When the researcher (or a hand-written workflow) references ComfyUI
nodes that the pod's ComfyUI doesn't have, install the providing
custom-node repo + its pip dependencies, then signal ComfyUI to
restart so it picks them up.

This is a catalog-based dispatcher. The catalog maps node class names
→ git repo + pip deps. Catalog is intentionally hand-curated (small,
trusted), not crowd-sourced — installing arbitrary repos on the pod
is a security risk. If the researcher cites a node not in the catalog,
the iter fails with a clear "node X requires unknown custom_nodes
repo; add it to NODE_CATALOG" message.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


# Node class → custom-node repo metadata
# Format: { class_name: (repo_url, repo_dirname, pip_requirements_path_in_repo) }
# repo_dirname is what `git clone <repo>` creates locally
# pip_requirements_path is None if no requirements.txt
NODE_CATALOG: dict[str, tuple[str, str, str | None]] = {
    # Impact Pack — FaceDetailer + bbox detector + SAM
    "FaceDetailer":                 ("https://github.com/ltdrdata/ComfyUI-Impact-Pack",
                                      "ComfyUI-Impact-Pack", "requirements.txt"),
    "UltralyticsDetectorProvider":  ("https://github.com/ltdrdata/ComfyUI-Impact-Pack",
                                      "ComfyUI-Impact-Pack", "requirements.txt"),
    "SAMLoader":                    ("https://github.com/ltdrdata/ComfyUI-Impact-Pack",
                                      "ComfyUI-Impact-Pack", "requirements.txt"),

    # Detail Daemon — sigma manipulation sampler
    "DetailDaemonSamplerNode":      ("https://github.com/Jonseed/ComfyUI-Detail-Daemon",
                                      "ComfyUI-Detail-Daemon", "requirements.txt"),

    # Ultimate SD Upscale
    "UltimateSDUpscale":            ("https://github.com/ssitu/ComfyUI_UltimateSDUpscale",
                                      "ComfyUI_UltimateSDUpscale", None),

    # CLIP GGUF loader (Qwen + others)
    "CLIPLoaderGGUF":               ("https://github.com/city96/ComfyUI-GGUF",
                                      "ComfyUI-GGUF", "requirements.txt"),

    # ControlNet aux (preprocessors)
    "AIO_Preprocessor":             ("https://github.com/Fannovel16/comfyui_controlnet_aux",
                                      "comfyui_controlnet_aux", "requirements.txt"),
    "OpenposePreprocessor":         ("https://github.com/Fannovel16/comfyui_controlnet_aux",
                                      "comfyui_controlnet_aux", "requirements.txt"),

    # Was Node Suite (kitchen sink)
    "Image Filter Adjustments":     ("https://github.com/WASasquatch/was-node-suite-comfyui",
                                      "was-node-suite-comfyui", "requirements.txt"),

    # InfuseNet — InfiniteYou-style identity preservation. As of 2026 there's
    # no canonical ComfyUI custom_node for it; researcher should propose
    # workflows that use it only if it actually finds a working repo. If
    # cited without a repo, the workflow will fail at submit time + get
    # blacklisted. Catalog entries below intentionally OMITTED; let the
    # ComfyUI rejection be the signal.

    # InstantID
    "InstantIDLoader":              ("https://github.com/cubiq/ComfyUI_InstantID",
                                      "ComfyUI_InstantID", "requirements.txt"),
    "InstantIDFaceAnalysis":        ("https://github.com/cubiq/ComfyUI_InstantID",
                                      "ComfyUI_InstantID", "requirements.txt"),
    "ApplyInstantID":               ("https://github.com/cubiq/ComfyUI_InstantID",
                                      "ComfyUI_InstantID", "requirements.txt"),

    # Florence2 (vision-language for caption/region tasks)
    "DownloadAndLoadFlorence2Model": ("https://github.com/kijai/ComfyUI-Florence2",
                                       "ComfyUI-Florence2", "requirements.txt"),
    "Florence2Run":                 ("https://github.com/kijai/ComfyUI-Florence2",
                                      "ComfyUI-Florence2", "requirements.txt"),

    # rgthree (commonly needed for graph operations Flux workflows use)
    "Image Comparer (rgthree)":     ("https://github.com/rgthree/rgthree-comfy",
                                      "rgthree-comfy", "requirements.txt"),

    # ClownsharKSampler / RES4LYF (advanced samplers)
    "ClownsharKSampler":            ("https://github.com/ClownsharkBatwing/RES4LYF",
                                      "RES4LYF", "requirements.txt"),

    # CLIPTextEncodeFlux — actually built into newer ComfyUI but include
    # as alias in case the catalog hits this name from older workflows
    # (no install needed; treat as builtin alias)
}


def required_nodes_in_workflow(workflow_path: Path) -> set[str]:
    """Scan a workflow JSON for class_type values. Tolerates the
    {placeholder} templating style by handling string-inside-quotes
    and bare numeric positions differently."""
    try:
        text = workflow_path.read_text()
        import re
        # "<{key}>"  → "x"     (placeholder inside string-quotes)
        text = re.sub(r'"\{[a-zA-Z_][a-zA-Z0-9_]*\}"', '"x"', text)
        # bare {key} → 0       (placeholder in numeric position)
        text = re.sub(r'\{[a-zA-Z_][a-zA-Z0-9_]*\}', '0', text)
        wf = json.loads(text)
    except (json.JSONDecodeError, OSError):
        return set()
    classes: set[str] = set()
    for node in wf.values():
        if isinstance(node, dict) and "class_type" in node:
            classes.add(node["class_type"])
    return classes


# These are ALWAYS in vanilla ComfyUI; never need installation.
BUILTIN_NODES = {
    "CheckpointLoaderSimple", "LoraLoader", "LoraLoaderModelOnly",
    "UNETLoader", "VAELoader", "CLIPLoader", "DualCLIPLoader",
    "CLIPTextEncode", "CLIPTextEncodeFlux", "EmptyLatentImage", "EmptySD3LatentImage",
    "KSampler", "KSamplerAdvanced", "SamplerCustom", "SamplerCustomAdvanced",
    "VAEDecode", "VAEEncode", "SaveImage", "PreviewImage", "LoadImage",
    "ImageScale", "ImageScaleBy", "LatentUpscale", "LatentUpscaleBy",
    "ModelSamplingAuraFlow", "ModelSamplingFlux", "ModelSamplingSD3",
    "BasicScheduler", "BasicGuider", "RandomNoise", "KSamplerSelect",
    "ControlNetLoader", "ControlNetApplyAdvanced",
    "CLIPVisionLoader", "CLIPVisionEncode",
    "UpscaleModelLoader",
    # PuLID-Flux (we have it installed via seed.sh)
    "PulidFluxModelLoader", "PulidFluxEvaClipLoader",
    "PulidFluxInsightFaceLoader", "ApplyPulidFlux",
    # IPAdapter plus (we have it installed)
    "IPAdapterUnifiedLoaderFaceID", "IPAdapterInsightFaceLoader",
    "IPAdapterFaceID", "IPAdapterUnifiedLoader", "IPAdapterAdvanced",
}


def missing_nodes(workflow_path: Path) -> tuple[set[str], set[str]]:
    """Returns (installable, unknown).
       installable: nodes we DO have a catalog entry for
       unknown:     nodes we don't recognize at all
    """
    required = required_nodes_in_workflow(workflow_path)
    installable = set()
    unknown = set()
    for cls in required:
        if cls in BUILTIN_NODES:
            continue
        if cls in NODE_CATALOG:
            installable.add(cls)
        else:
            unknown.add(cls)
    return installable, unknown


def install_script(installable_nodes: set[str]) -> str:
    """Build a bash script that clones the needed repos + installs deps.
    Run this on the pod via ssh. Idempotent (won't re-clone if dir exists).
    """
    repos: dict[str, tuple[str, str | None]] = {}
    for cls in installable_nodes:
        url, dirname, req = NODE_CATALOG[cls]
        repos[dirname] = (url, req)

    # Intentionally NO `set -e`: one bad clone (404, network blip,
    # auth required) must NOT abort the whole install. Each clone runs
    # under its own `|| true` and we log failures.
    lines = [
        "cd /runpod-volume/ComfyUI/custom_nodes || exit 1",
        "FAILED_CLONES=''",
    ]
    for dirname, (url, req) in repos.items():
        lines.append(f'if [ ! -d "{dirname}" ]; then')
        lines.append(f'  echo "[ensure_nodes] cloning {dirname} from {url}"')
        lines.append(f'  if git clone --depth 1 {url} {dirname}; then')
        lines.append(f'    echo "[ensure_nodes] cloned {dirname}"')
        lines.append(f'  else')
        lines.append(f'    echo "[ensure_nodes] FAILED to clone {dirname} ({url})"')
        lines.append(f'    FAILED_CLONES="$FAILED_CLONES {dirname}"')
        lines.append(f'    continue')
        lines.append(f'  fi')
        lines.append(f'else')
        lines.append(f'  echo "[ensure_nodes] {dirname} already present, updating"')
        lines.append(f'  (cd {dirname} && git pull --quiet) || true')
        lines.append(f'fi')
        if req:
            lines.append(f'if [ -f "{dirname}/{req}" ]; then')
            lines.append(f'  python3.11 -m pip install --quiet -r "{dirname}/{req}" || true')
            lines.append(f'fi')
    lines.append('if [ -n "$FAILED_CLONES" ]; then')
    lines.append('  echo "[ensure_nodes] some clones failed:$FAILED_CLONES (their nodes will be unavailable)"')
    lines.append('fi')
    lines.append('exit 0')  # always succeed at script level
    return "\n".join(lines)
