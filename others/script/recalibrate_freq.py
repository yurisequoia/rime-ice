#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "torch>=2.7",
#   "transformers>=4.57.0",
#   "accelerate>=1.0",
#   "numpy>=1.26",
#   "scipy>=1.12",
#   "scikit-learn>=1.4",
#   "huggingface-hub>=0.25",
#   "safetensors>=0.4",
#   "rime-qwen-freq @ git+https://github.com/mizorewww/rime-qwen-freq.git",
# ]
#
# [[tool.uv.index]]
# name = "pytorch-cu128"
# url = "https://download.pytorch.org/whl/cu128"
# explicit = true
#
# [tool.uv.sources]
# torch = { index = "pytorch-cu128" }
# ///
"""
固化参数的 Rime 词频离线校准脚本（本地 GPU 一键全量/增量校准，不接触 %APPDATA%\\Rime）。

日常半年后增量补打新词：
    uv run others/script/recalibrate_freq.py

强制全量重算所有目标词库：
    uv run others/script/recalibrate_freq.py --full
"""
from __future__ import annotations

import argparse
import gc
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request

sys.dont_write_bytecode = True

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "120")

ANCHOR_FILES = (
    ("cn_dicts/base.dict.yaml", 0),
    ("cn_dicts/8105.dict.yaml", 10),
)
DEFAULT_MODEL = "Qwen/Qwen3-8B-Base"
DEFAULT_GENERAL_PROMPT = "日常与百科常见词条："
DEFAULT_DOMAIN_PROMPT = "二次元作品与计算机技术术语："


def _load_apply_module():
    path = Path(__file__).with_name("apply_freq_cache.py")
    spec = importlib.util.spec_from_file_location("apply_freq_cache", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["apply_freq_cache"] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_existing_cache(afc, cache_path: Path) -> dict[str, int]:
    if not cache_path.is_file():
        return {}
    try:
        cache, _ = afc.load_freq_cache(cache_path)
        return cache
    except ValueError:
        return {}


def _write_cache_gz(cache_path: Path, weights_map: dict[str, int]) -> None:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6, mtime=0) as gz:
        text = "".join(f"{w}\t{wt}\n" for w, wt in sorted(weights_map.items()))
        gz.write(text.encode("utf-8"))
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(buf.getvalue())


def _quantize_backbone_int8(backbone, lm_head, torch, nn, F):
    """Replace backbone nn.Linear layers with symmetric per-channel INT8 weights to fit 16 GiB VRAM."""

    class Int8WeightOnlyLinear(nn.Module):
        def __init__(self, linear: nn.Linear) -> None:
            super().__init__()
            with torch.no_grad():
                orig_dtype = linear.weight.dtype
                w_f = linear.weight.detach().float()
                scale_f = w_f.abs().amax(dim=1, keepdim=True).clamp_min(1e-8) / 127.0
                q = torch.clamp(torch.round(w_f / scale_f), -127, 127).to(torch.int8)
                scale = scale_f.to(orig_dtype)
            self.register_buffer("weight_int8", q)
            self.register_buffer("scale", scale)
            if linear.bias is not None:
                self.register_buffer("bias", linear.bias.detach().clone())
            else:
                self.bias = None

        def forward(self, x: torch.Tensor) -> torch.Tensor:
            w = self.weight_int8.to(x.dtype) * self.scale
            return F.linear(x, w, self.bias)

    replaced = 0

    def _walk(module: nn.Module) -> None:
        nonlocal replaced
        for child_name in list(module._modules.keys()):
            child = module._modules[child_name]
            if child is None or child is lm_head:
                continue
            if isinstance(child, nn.Linear):
                module._modules[child_name] = Int8WeightOnlyLinear(child)
                del child
                replaced += 1
            else:
                _walk(child)

    _walk(backbone)
    return replaced


def _resolve_model_path(model_id: str, source: str) -> str:
    """Resolve a local model directory or download from ModelScope/HF into local cache."""
    local_path = Path(model_id)
    if local_path.exists():
        return str(local_path.resolve())
    if source == "hf":
        return model_id

    cache_dir = Path.home() / ".cache" / "rime-qwen-freq" / "models" / model_id.replace("/", "--")
    cache_dir.mkdir(parents=True, exist_ok=True)
    api_url = f"https://modelscope.cn/api/v1/models/{model_id}/repo/files?Revision=master"
    req = urllib.request.Request(api_url, headers={"User-Agent": "rime-qwen-freq-calibrator"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    files = payload.get("Data", {}).get("Files", [])
    if not files:
        raise RuntimeError(f"ModelScope 仓库文件列表为空: {model_id}")

    curl_bin = shutil.which("curl.exe") or shutil.which("curl")
    if not curl_bin:
        raise RuntimeError("未找到 curl 可执行文件，无法从 ModelScope 断点续传下载模型")

    for entry in files:
        name = entry.get("Name", "")
        if not name or name in (".gitattributes", "README.md") or entry.get("Type") == "tree":
            continue
        expected_size = int(entry.get("Size", 0))
        dest = cache_dir / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_file() and expected_size > 0 and dest.stat().st_size == expected_size:
            continue
        url = f"https://modelscope.cn/models/{model_id}/resolve/master/{name}"
        print(f"Downloading {name} ({expected_size / (1024 ** 2):.1f} MiB) from ModelScope ...", flush=True)
        subprocess.run([curl_bin, "-f", "-L", "-C", "-", "--retry", "5", "-o", str(dest), url], check=True)
    return str(cache_dir)


def _load_model_for_scoring(model_id: str, device: str, model_source: str = "modelscope"):
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from rime_qwen_freq.scorer import (
        extract_backbone_and_head,
        select_device_and_dtype,
        verify_trie_exactness,
    )

    dev, dtype = select_device_and_dtype(device)
    dev_label = torch.cuda.get_device_name(0) if dev.startswith("cuda") else dev

    quantize_int8 = False
    free_gib = None
    if dev.startswith("cuda") and torch.cuda.is_available():
        free_bytes, total_bytes = torch.cuda.mem_get_info(0)
        free_gib = free_bytes / (1024 ** 3)
        total_gib = total_bytes / (1024 ** 3)
        print(
            f"CUDA device: {dev_label} | free={free_gib:.2f} GiB / total={total_gib:.2f} GiB",
            flush=True,
        )
        if free_gib < 16.5:
            quantize_int8 = True

    resolved_model = _resolve_model_path(model_id, model_source)
    print(f"Loading tokenizer & model {resolved_model} (dtype={dtype}, int8_backbone={quantize_int8}) ...", flush=True)
    tok = AutoTokenizer.from_pretrained(resolved_model)
    if quantize_int8:
        full_model = AutoModelForCausalLM.from_pretrained(
            resolved_model, dtype=dtype, low_cpu_mem_usage=True
        )
        backbone, lm_head = extract_backbone_and_head(full_model)
        target_root = backbone if backbone is not None else full_model
        replaced = _quantize_backbone_int8(target_root, lm_head, torch, nn, F)
        gc.collect()
        print(f"Quantized {replaced} backbone Linear layers to symmetric INT8; moving to {dev} ...", flush=True)
        full_model.to(dev)
        gc.collect()
        if torch.cuda.is_available():
            alloc_gib = torch.cuda.memory_allocated(0) / (1024 ** 3)
            print(f"Model on {dev} | VRAM allocated={alloc_gib:.2f} GiB", flush=True)
    else:
        load_kwargs = {"dtype": dtype}
        if dev.startswith("cuda"):
            load_kwargs["device_map"] = dev
            full_model = AutoModelForCausalLM.from_pretrained(resolved_model, **load_kwargs)
        else:
            full_model = AutoModelForCausalLM.from_pretrained(resolved_model, **load_kwargs).to(dev)
        backbone, lm_head = extract_backbone_and_head(full_model)

    full_model.eval()
    max_err = verify_trie_exactness(tok, full_model, backbone, lm_head, device=dev)
    print(f"Prefix-Trie equivalence verified (max_abs_diff={max_err:.5f})", flush=True)
    return tok, full_model, backbone, lm_head, dev


def main() -> int:
    afc = _load_apply_module()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rime-path",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Rime 仓库根目录（默认自动定位）",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path(afc.DEFAULT_CACHE_REL),
        help="词频缓存 .tsv.gz 路径（默认 others/freq_cache.tsv.gz）",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"CausalLM 模型或本地路径（默认 {DEFAULT_MODEL}）")
    parser.add_argument(
        "--model-source",
        choices=("modelscope", "hf"),
        default="modelscope",
        help="远程模型下载源（默认 modelscope）",
    )
    parser.add_argument("--device", default="auto", help="设备: auto | cuda | cpu")
    parser.add_argument("--max-batch", type=int, default=512, help="最大前缀树 batch（默认 512）")
    parser.add_argument("--cn-anchors-max", type=int, default=100_000, help="锚点抽样上限（默认 100000）")
    parser.add_argument("--seed", type=int, default=42, help="随机种子（默认 42）")
    parser.add_argument("--general-prompt", default=DEFAULT_GENERAL_PROMPT)
    parser.add_argument("--domain-prompt", default=DEFAULT_DOMAIN_PROMPT)
    parser.add_argument("--domain-threshold", type=float, default=1.3)
    parser.add_argument("--domain-slope", type=float, default=0.135)
    parser.add_argument("--domain-max-boost", type=float, default=0.54)
    parser.add_argument("--full", action="store_true", help="全量重打全部目标词库")
    parser.add_argument("--smoke-test", action="store_true", help="仅验证 CUDA 与 Prefix-Trie 数值等价性后退出")
    args = parser.parse_args()

    rime_path = args.rime_path.resolve()
    cache_path = args.cache if args.cache.is_absolute() else (rime_path / args.cache)

    if args.smoke_test:
        _load_model_for_scoring(args.model, args.device, args.model_source)
        print("Smoke test passed.", flush=True)
        return 0

    from rime_qwen_freq.dicts import iter_dict_entries, load_clean_weights, stratified_sample_anchors
    from rime_qwen_freq.scorer import (
        batch_tokenize,
        build_prefix_trie,
        build_single_context_features,
        calibrate_weights,
        compute_domain_lift,
        trie_score_pass,
    )

    all_target_words: set[str] = set()
    for rel in afc.DEFAULT_TARGETS:
        p = rime_path / rel
        if not p.is_file():
            raise FileNotFoundError(f"找不到目标词库: {p}")
        for word, code, _ in iter_dict_entries(p):
            w_clean = word.strip()
            if w_clean and code:
                all_target_words.add(w_clean)

    existing_cache = {} if args.full else _load_existing_cache(afc, cache_path)
    if args.full or not existing_cache:
        mode = "full"
        words_to_score = all_target_words
    else:
        mode = "incremental"
        words_to_score = {w for w in all_target_words if w not in existing_cache}

    print(
        f"Mode: {mode} | target unique words={len(all_target_words):,} | "
        f"existing cache={len(existing_cache):,} | to_score={len(words_to_score):,}",
        flush=True,
    )

    if not words_to_score:
        print("No new unscored words found; running cache verification apply ...", flush=True)
        stats_list, unscored, fallback_p25 = afc.apply_cache_to_targets(
            rime_path, cache_path, afc.DEFAULT_TARGETS
        )
        print(afc.format_report(stats_list, len(unscored), fallback_p25, len(existing_cache)), end="")
        return 0

    cn_gt_pool: dict[str, int] = {}
    for rel, min_char_w in ANCHOR_FILES:
        anchor_path = rime_path / rel
        if not anchor_path.is_file():
            raise FileNotFoundError(f"找不到锚点词库: {anchor_path}")
        d = load_clean_weights(anchor_path, single_char_min_weight=min_char_w)
        for k, v in d.items():
            if k not in cn_gt_pool:
                cn_gt_pool[k] = v
        print(f"Loaded anchor {rel}: {len(d):,} valid weighted words (pool={len(cn_gt_pool):,})", flush=True)

    cn_anchors = stratified_sample_anchors(cn_gt_pool, args.cn_anchors_max, seed=args.seed)
    cn_words = sorted(words_to_score | set(cn_anchors.keys()))
    print(
        f"Combined scoring set: {len(cn_words):,} words "
        f"(targets={len(words_to_score):,}, anchors={len(cn_anchors):,})",
        flush=True,
    )

    tok, full_model, backbone, lm_head, dev = _load_model_for_scoring(
        args.model, args.device, args.model_source
    )
    cn_seqs = batch_tokenize(tok, cn_words)
    cn_trie = build_prefix_trie(cn_seqs)
    print(
        f"Prefix Trie compressed {len(cn_words):,} words -> {len(cn_trie['leaves']):,} leaf paths",
        flush=True,
    )

    cn_f_gen = trie_score_pass(
        tok, full_model, backbone, lm_head, cn_trie, args.general_prompt, device=dev, max_batch=args.max_batch
    )
    cn_f_dom = trie_score_pass(
        tok, full_model, backbone, lm_head, cn_trie, args.domain_prompt, device=dev, max_batch=args.max_batch
    )
    del cn_trie

    cn_delta_domain = compute_domain_lift(cn_f_dom[:, 0], cn_f_gen[:, 0])
    X_cn = build_single_context_features(cn_words, cn_seqs, cn_f_gen)
    cn_weights, cn_metrics = calibrate_weights(
        cn_words,
        X_cn,
        cn_anchors,
        log_space=True,
        delta_domain=cn_delta_domain,
        domain_threshold=args.domain_threshold,
        domain_slope=args.domain_slope,
        domain_max_boost=args.domain_max_boost,
    )
    print(f"Calibration metrics: {cn_metrics}", flush=True)

    scored_map = {w: int(wt) for w, wt in zip(cn_words, cn_weights) if w in words_to_score}
    merged_cache = dict(existing_cache)
    merged_cache.update(scored_map)
    _write_cache_gz(cache_path, merged_cache)
    cache_mib = cache_path.stat().st_size / (1024 ** 2)
    print(f"Saved {len(merged_cache):,} cached weights to {cache_path} ({cache_mib:.2f} MiB)", flush=True)

    stats_list, unscored, fallback_p25 = afc.apply_cache_to_targets(
        rime_path, cache_path, afc.DEFAULT_TARGETS
    )
    print(afc.format_report(stats_list, len(unscored), fallback_p25, len(merged_cache)), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
