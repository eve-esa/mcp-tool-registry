# ESA MOOCs MCP image: same layout as ../../shared/Dockerfile, with three
# divergences forced by the AgentCore Runtime quota "Maximum size (in MB) for a
# Docker image" = 2048 MB, which is NOT adjustable. The shared build produced a
# ~3.06 GB image and CreateAgentRuntime failed with ServiceQuotaExceededException.
#   1. torch is installed from the PyTorch CPU-only index (see the RUN below);
#   2. the two HuggingFace checkpoints are baked in at build time and the runtime
#      is pinned offline, so a cold microVM never reaches the Hub;
#   3. torch's build-time-only ballast (headers, test tree, static archives) is
#      stripped in the same layer that installs it.
FROM python:3.13-slim

WORKDIR /app

# Fixed absolute cache path so the build-time download and the runtime load agree
# regardless of $HOME or uid. Must be set BEFORE the prefetch RUN below.
# HF_HOME covers huggingface_hub, transformers and sentence-transformers at once;
# do not also set SENTENCE_TRANSFORMERS_HOME to a different path.
ENV HF_HOME=/opt/hf \
    HF_HUB_DISABLE_TELEMETRY=1

COPY requirements.txt .

# torch is installed first, from the CPU-only index, before requirements.txt can
# pull the fat one in as a sentence-transformers dependency.
#
# Root cause of the 3 GB image: from torch 2.11.0 the plain PyPI aarch64 wheel
# bundles CUDA (sbsa) and its nvidia-cu13 dependencies lost the
# `platform_machine == "x86_64"` marker guard, so an arm64 build now drags in
# cublas/cudnn/nccl/nvshmem/triton — ~2.8 GB of wheels we can never execute.
#
# The `+cpu` local version tag is what actually binds the source. pip merges
# candidates from every index and picks by VERSION, not by index priority, so an
# `--index-url` alone silently falls back to the fat PyPI wheel as soon as PyPI
# ships a version the CPU index has not mirrored yet. `==2.13.0+cpu` cannot match
# any PyPI wheel, so it can only resolve here.
#
# The index URL lives in this Dockerfile and NOT in requirements.txt on purpose:
# scripts/validate_pr.py::check_requirements_txt rejects any line without a
# version operator (so `--index-url ...` fails the gate), and the pr-checks
# `validate-structure` job runs a plain `pip install -r requirements.txt` against
# PyPI, where `torch==2.13.0+cpu` does not exist. requirements.txt stays portable.
#
# The trims are build-time-only artifacts (~150 MB): C++ headers, the test tree
# and static archives. Do NOT remove torch/bin — torch_shm_manager is loaded at
# import time. The asserts fail the build rather than shipping a silent 3 GB
# regression, same convention as ../effis/Dockerfile.
RUN pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cpu \
        torch==2.13.0+cpu \
    && pip install --no-cache-dir -r requirements.txt \
    && python -c "import torch; assert torch.__version__.endswith('+cpu'), torch.__version__; assert not torch.cuda.is_available()" \
    && ! pip list --format=freeze 2>/dev/null | grep -iqE '^(nvidia|triton|cuda)' \
    && SP="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" \
    && rm -rf "$SP/torch/test" "$SP/torch/include" \
    && find "$SP/torch/lib" -name '*.a' -delete

# Bake both checkpoints. AgentCore microVMs start cold with no persistent cache,
# so a runtime download would stall the first tools/call by minutes.
# Only the files actually loaded are fetched: the reranker repo also ships
# pytorch_model.bin, flax_model.msgpack, onnx/ and openvino/ variants — 761 MiB
# of dead weight that a bare snapshot_download would copy in.
#
# The repo id strings must stay byte-identical to server.py:35-36. The hub cache
# directory is keyed by the *requested* id, not the canonical one:
# `cross-encoder/ms-marco-MiniLM-L-6-v2` now redirects to `...MiniLM-L6-v2`, and
# prefetching under the canonical name would be a cache MISS at runtime, which
# under HF_HUB_OFFLINE=1 is a hard failure instead of a slow download.
#
# Both are pinned to a commit. `main` is a moving target: without this the image
# is not reproducible (rebuild this commit next month, get different weights and
# a different size) and an upstream force-push changes what the tool answers with
# no signal at all. The 40-char SHAs are safe here — validate_pr.py's
# hardcoded-secret scan only reads .py files under servers/.
#
# The check afterwards LOADS the models rather than looking for files. An earlier
# version tested for refs/main, which proves nothing: snapshot_download writes
# refs/ and trees/ from repo metadata BEFORE it filters by allow_patterns, so
# that test passes even when zero weights were fetched (verified: a deliberately
# impossible allow_patterns still created refs/main and still exited 0). An
# upstream rename — the embedder moving to model.safetensors, the reranker
# dropping vocab.txt — would then build green and fail on the first tools/call,
# which is the exact failure this whole prefetch exists to prevent. Asserting the
# loaders is the same convention as ../effis/Dockerfile:29, which tests for the
# shapefile it actually reads.
# refs/main has to be written by hand. Downloading by SHA populates snapshots/<sha>/
# but writes no ref, and an offline load by repo id resolves the default revision
# THROUGH refs/main — so pinning without this line yields "couldn't connect to
# huggingface.co ... and couldn't find them in the cached files" at load. Writing
# the ref is what the hub itself does for a named revision, and it keeps both
# properties: reproducible weights, and server.py loading by plain repo id.
RUN python -c "\
import pathlib; \
from huggingface_hub import snapshot_download as d; \
PINS = { \
    'nasa-impact/nasa-smd-ibm-st-v2': ('e4a3e22048f0173ef150470cfbf67e9e8aaf4f8c', ['config.json','modules.json','sentence_bert_config.json','1_Pooling/config.json','pytorch_model.bin','tokenizer.json','tokenizer_config.json','special_tokens_map.json','vocab.json','merges.txt']), \
    'cross-encoder/ms-marco-MiniLM-L-6-v2': ('c5ee24cb16019beea0893ab7796b1df96625c6b8', ['config.json','model.safetensors','tokenizer.json','tokenizer_config.json','special_tokens_map.json','vocab.txt']), \
}; \
[ ( d(repo, revision=sha, allow_patterns=pats), \
    (lambda r: (r.mkdir(parents=True, exist_ok=True), (r / 'main').write_text(sha)))( \
        pathlib.Path('/opt/hf/hub') / ('models--' + repo.replace('/', '--')) / 'refs') ) \
  for repo, (sha, pats) in PINS.items() ]" \
    && rm -rf /opt/hf/xet /opt/hf/hub/.locks \
    && HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python -c "\
from sentence_transformers import SentenceTransformer, CrossEncoder; \
m = SentenceTransformer('nasa-impact/nasa-smd-ibm-st-v2'); \
assert m.get_sentence_embedding_dimension() == 768, m.get_sentence_embedding_dimension(); \
CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2').predict([('a', 'b')])"

# From here on the container must never reach the Hub. Set AFTER the prefetch.
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

COPY . .

EXPOSE 8000

CMD ["python", "server.py"]
