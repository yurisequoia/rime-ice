#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""在 GitHub Actions 中增量检测上游语法模型更新，合并并发布到固定 Release tag (`gram`)。"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
import urllib.error
import urllib.request

WANXIANG_RELEASE_API = "https://api.github.com/repos/amzxyz/RIME-LMDG/releases/tags/LTS"
QWEN_RELEASE_API = "https://api.github.com/repos/mizorewww/rime-qwen-freq/releases/latest"
WANXIANG_ASSET_NAME = "wanxiang-lts-zh-hans.gram"
QWEN_ASSET_NAME = "qwen14b-zh-hans.gram.gz"
OUTPUT_GRAM_NAME = "wanxiang-qwen14b-zh-hans.gram"
RELEASE_TAG = "gram"
MIN_KEEP_VAL = 140_000
SAMPLE_QUERIES = ("人工智能", "不假思索", "的话$")


def _api_get_json(url: str, token: str | None) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "rime-ice-hybrid-gram-builder",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _find_asset(release_json: dict, name: str) -> dict:
    for asset in release_json.get("assets", []):
        if asset.get("name") == name:
            return {
                "name": name,
                "url": asset["browser_download_url"],
                "size": asset.get("size"),
                "digest": asset.get("digest"),
                "updated_at": asset.get("updated_at"),
                "tag_name": release_json.get("tag_name"),
            }
    raise RuntimeError(f"Release {release_json.get('tag_name')} 中找不到资源: {name}")


def _download_file(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "rime-ice-hybrid-gram-builder"})
    with urllib.request.urlopen(req, timeout=300) as resp, dest.open("wb") as fout:
        shutil.copyfileobj(resp, fout, length=1024 * 1024)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _write_summary(lines: list[str]) -> None:
    text = "\n".join(lines) + "\n"
    print(text, end="")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text)


def _get_existing_release_manifest(repo: str, tmp_dir: Path) -> tuple[bool, bool, dict | None]:
    proc = subprocess.run(
        ["gh", "release", "view", RELEASE_TAG, "--repo", repo, "--json", "assets"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return False, False, None
    data = json.loads(proc.stdout)
    asset_names = {a["name"] for a in data.get("assets", [])}
    has_gram = OUTPUT_GRAM_NAME in asset_names
    if "manifest.json" not in asset_names:
        return True, has_gram, None
    dl = subprocess.run(
        [
            "gh", "release", "download", RELEASE_TAG,
            "--repo", repo, "--pattern", "manifest.json", "--dir", str(tmp_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if dl.returncode != 0 or not (tmp_dir / "manifest.json").is_file():
        return True, has_gram, None
    try:
        return True, has_gram, json.loads((tmp_dir / "manifest.json").read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return True, has_gram, None


def _asset_fingerprint(info: dict) -> tuple:
    return (info.get("tag_name"), info.get("digest"), info.get("updated_at"), info.get("size"))


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        raise RuntimeError("缺少 GITHUB_REPOSITORY 环境变量")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    force_rebuild = os.environ.get("FORCE_REBUILD", "false").strip().lower() == "true"

    wanxiang_meta = _find_asset(_api_get_json(WANXIANG_RELEASE_API, token), WANXIANG_ASSET_NAME)
    qwen_meta = _find_asset(_api_get_json(QWEN_RELEASE_API, token), QWEN_ASSET_NAME)

    with tempfile.TemporaryDirectory(prefix="hybrid_gram_ci_") as tmp_str:
        tmp_dir = Path(tmp_str)
        release_exists, has_gram, old_manifest = _get_existing_release_manifest(repo, tmp_dir)

        if (
            not force_rebuild
            and release_exists
            and has_gram
            and old_manifest is not None
            and old_manifest.get("min_keep_val") == MIN_KEEP_VAL
            and _asset_fingerprint(old_manifest.get("wanxiang", {})) == _asset_fingerprint(wanxiang_meta)
            and _asset_fingerprint(old_manifest.get("qwen14b", {})) == _asset_fingerprint(qwen_meta)
        ):
            _write_summary([
                "### 融合语法模型检查结果：上游未更新，跳过重建",
                "",
                f"- 固定下载地址：`https://github.com/{repo}/releases/download/{RELEASE_TAG}/{OUTPUT_GRAM_NAME}`",
                f"- Wanxiang (`{wanxiang_meta['tag_name']}`): `{wanxiang_meta['updated_at']}`",
                f"- Qwen14B (`{qwen_meta['tag_name']}`): `{qwen_meta['updated_at']}`",
            ])
            return 0

        wanxiang_path = tmp_dir / WANXIANG_ASSET_NAME
        qwen_gz_path = tmp_dir / QWEN_ASSET_NAME
        qwen_path = tmp_dir / "qwen14b-zh-hans.gram"
        out_path = tmp_dir / OUTPUT_GRAM_NAME
        merge_bin = tmp_dir / "merge_gram"

        print(f"Downloading {wanxiang_meta['url']} ...", flush=True)
        _download_file(wanxiang_meta["url"], wanxiang_path)
        print(f"Downloading {qwen_meta['url']} ...", flush=True)
        _download_file(qwen_meta["url"], qwen_gz_path)

        with gzip.open(qwen_gz_path, "rb") as fin, qwen_path.open("wb") as fout:
            shutil.copyfileobj(fin, fout, length=1024 * 1024)

        gram_dir = repo_root / "others" / "script" / "gram"
        subprocess.run(
            [
                "g++", "-O3", "-std=c++17",
                str(gram_dir / "merge_gram.cc"),
                f"-I{gram_dir}",
                "-o", str(merge_bin),
            ],
            check=True,
        )

        merge_proc = subprocess.run(
            [str(merge_bin), "merge", str(wanxiang_path), str(qwen_path), str(MIN_KEEP_VAL), str(out_path)],
            capture_output=True,
            text=True,
            check=True,
        )
        print(merge_proc.stderr, end="")

        size_mib = out_path.stat().st_size / (1024 ** 2)
        if size_mib < 100.0:
            raise RuntimeError(f"产物 {OUTPUT_GRAM_NAME} 体积异常偏小: {size_mib:.2f} MiB")

        query_proc = subprocess.run(
            [str(merge_bin), "query", str(out_path), "-"],
            input="\n".join(SAMPLE_QUERIES) + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        print("Sample queries:\n" + query_proc.stdout, end="")

        out_sha256 = _sha256_file(out_path)
        sums_path = tmp_dir / "SHA256SUMS.txt"
        sums_path.write_text(f"{out_sha256}  {OUTPUT_GRAM_NAME}\n", encoding="utf-8")

        manifest = {
            "output": OUTPUT_GRAM_NAME,
            "sha256": out_sha256,
            "size": out_path.stat().st_size,
            "min_keep_val": MIN_KEEP_VAL,
            "wanxiang": wanxiang_meta,
            "qwen14b": qwen_meta,
        }
        manifest_path = tmp_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        notes = (
            "万象 LTS 语法模型（剪除 `<140000` 弱尾项）与 `Qwen3-14B-Base` 蒸馏语法模型（`qwen14b-zh-hans`）自动融合产物。\n\n"
            f"- Wanxiang 来源：`{wanxiang_meta['tag_name']}` (`{wanxiang_meta['updated_at']}`)\n"
            f"- Qwen14B 来源：`{qwen_meta['tag_name']}` (`{qwen_meta['updated_at']}`)\n"
            f"- 体积：`{size_mib:.2f} MiB`\n"
            f"- SHA256：`{out_sha256}`\n"
        )
        if not release_exists:
            subprocess.run(
                [
                    "gh", "release", "create", RELEASE_TAG,
                    "--repo", repo,
                    "--title", "Hybrid Grammar Model (Wanxiang + Qwen14B)",
                    "--notes", notes,
                ],
                check=True,
            )
        else:
            subprocess.run(
                ["gh", "release", "edit", RELEASE_TAG, "--repo", repo, "--notes", notes],
                check=True,
            )

        subprocess.run(
            [
                "gh", "release", "upload", RELEASE_TAG,
                "--repo", repo, "--clobber",
                str(out_path), str(sums_path), str(manifest_path),
            ],
            check=True,
        )

        _write_summary([
            "### 融合语法模型已构建并更新 Release",
            "",
            f"- 固定下载地址：`https://github.com/{repo}/releases/download/{RELEASE_TAG}/{OUTPUT_GRAM_NAME}`",
            f"- 文件体积：`{size_mib:.2f} MiB`",
            f"- SHA256：`{out_sha256}`",
            f"- Wanxiang：`{wanxiang_meta['tag_name']}` (`{wanxiang_meta['updated_at']}`)",
            f"- Qwen14B：`{qwen_meta['tag_name']}` (`{qwen_meta['updated_at']}`)",
        ])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
