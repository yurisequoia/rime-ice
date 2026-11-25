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


def _download_verified(asset: dict, dest: Path) -> None:
    _download_file(asset["url"], dest)
    if dest.stat().st_size != asset["size"]:
        raise RuntimeError(f"上游文件大小不匹配: {asset['name']}")
    if asset["digest"] and asset["digest"] != f"sha256:{_sha256_file(dest)}":
        raise RuntimeError(f"上游文件 SHA-256 不匹配: {asset['name']}")


def _write_summary(lines: list[str]) -> None:
    text = "\n".join(lines) + "\n"
    print(text, end="")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text)


def _get_release_assets(repo: str, token: str | None) -> dict[str, dict] | None:
    url = f"https://api.github.com/repos/{repo}/releases/tags/{RELEASE_TAG}"
    try:
        release = _api_get_json(url, token)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    return {asset["name"]: asset for asset in release["assets"]}


def _checksum_text(digest: str) -> str:
    return f"{digest}  {OUTPUT_GRAM_NAME}\n"


def _matches_asset(asset: dict, digest: str) -> bool:
    return asset.get("state") == "uploaded" and asset.get("digest") == f"sha256:{digest}"


def _verified_manifest(repo: str, tmp_dir: Path, assets: dict[str, dict]) -> dict | None:
    if not all(name in assets for name in (OUTPUT_GRAM_NAME, "manifest.json", "SHA256SUMS.txt")):
        return None
    subprocess.run(
        [
            "gh", "release", "download", RELEASE_TAG,
            "--repo", repo, "--pattern", "manifest.json", "--dir", str(tmp_dir),
        ],
        check=True,
    )
    manifest_path = tmp_dir / "manifest.json"
    if not _matches_asset(assets["manifest.json"], _sha256_file(manifest_path)):
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(manifest, dict) or manifest.get("output") != OUTPUT_GRAM_NAME:
        return None
    gram = assets[OUTPUT_GRAM_NAME]
    digest = manifest.get("sha256")
    if not isinstance(digest, str) or not _matches_asset(gram, digest) or gram["size"] != manifest.get("size"):
        return None
    sums_digest = hashlib.sha256(_checksum_text(digest).encode("utf-8")).hexdigest()
    return manifest if _matches_asset(assets["SHA256SUMS.txt"], sums_digest) else None


def _rename_asset(repo: str, asset: dict, name: str) -> None:
    subprocess.run(
        ["gh", "api", "--method", "PATCH", f"repos/{repo}/releases/assets/{asset['id']}", "-f", f"name={name}"],
        stdout=subprocess.DEVNULL,
        check=True,
    )


def _delete_asset(repo: str, asset: dict) -> None:
    subprocess.run(
        ["gh", "api", "--method", "DELETE", f"repos/{repo}/releases/assets/{asset['id']}"],
        check=True,
    )


def _restore_interrupted_promotions(repo: str, assets: dict[str, dict]) -> None:
    for name in (OUTPUT_GRAM_NAME, "SHA256SUMS.txt", "manifest.json"):
        previous_name = f"previous-{name}"
        if name not in assets and previous_name in assets:
            _rename_asset(repo, assets[previous_name], name)
            assets[name] = assets.pop(previous_name)


def _publish_assets(repo: str, token: str | None, assets: dict[str, dict], files: tuple[Path, ...], notes: str) -> None:
    expected = {file.name: _sha256_file(file) for file in files}
    pending = [file for file in files if not _matches_asset(assets.get(file.name, {}), expected[file.name])]
    if pending:
        staged_files = []
        for file in pending:
            staged = file.with_name(f"staged-{file.name}")
            if _matches_asset(assets.get(staged.name, {}), expected[file.name]):
                continue
            file.rename(staged)
            staged_files.append(str(staged))
        if staged_files:
            subprocess.run(
                ["gh", "release", "upload", RELEASE_TAG, "--repo", repo, "--clobber", *staged_files],
                check=True,
            )
            assets = _get_release_assets(repo, token) or {}
        for file in pending:
            if not _matches_asset(assets.get(f"staged-{file.name}", {}), expected[file.name]):
                raise RuntimeError(f"暂存资产 SHA-256 不匹配: {file.name}")

        for file in pending:
            name = file.name
            previous_name = f"previous-{name}"
            if name in assets and previous_name in assets:
                _delete_asset(repo, assets[previous_name])
            try:
                if name in assets:
                    _rename_asset(repo, assets[name], previous_name)
                _rename_asset(repo, assets[f"staged-{name}"], name)
            except subprocess.CalledProcessError:
                current = _get_release_assets(repo, token) or {}
                if name not in current and previous_name in current:
                    _rename_asset(repo, current[previous_name], name)
                raise

    current = _get_release_assets(repo, token) or {}
    if not all(_matches_asset(current.get(name, {}), digest) for name, digest in expected.items()):
        raise RuntimeError("发布后的语法模型资产缺失或 SHA-256 不匹配")
    subprocess.run(["gh", "release", "edit", RELEASE_TAG, "--repo", repo, "--notes", notes], check=True)
    for name in expected:
        previous = current.get(f"previous-{name}")
        if previous is not None:
            _delete_asset(repo, previous)


def _asset_fingerprint(info: dict) -> tuple:
    return (info.get("tag_name"), info.get("digest"), info.get("updated_at"), info.get("size"))


def main() -> int:
    gram_dir = Path(__file__).resolve().with_name("gram")
    merger_version = _sha256_file(gram_dir / "merge_gram.cc") + _sha256_file(gram_dir / "darts.h")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        raise RuntimeError("缺少 GITHUB_REPOSITORY 环境变量")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    force_rebuild = os.environ.get("FORCE_REBUILD", "false").strip().lower() == "true"
    assets = _get_release_assets(repo, token)
    if assets:
        _restore_interrupted_promotions(repo, assets)

    wanxiang_meta = _find_asset(_api_get_json(WANXIANG_RELEASE_API, token), WANXIANG_ASSET_NAME)
    qwen_meta = _find_asset(_api_get_json(QWEN_RELEASE_API, token), QWEN_ASSET_NAME)

    with tempfile.TemporaryDirectory(prefix="hybrid_gram_ci_") as tmp_str:
        tmp_dir = Path(tmp_str)
        old_manifest = _verified_manifest(repo, tmp_dir, assets) if assets and not force_rebuild else None

        if (
            old_manifest is not None
            and old_manifest.get("min_keep_val") == MIN_KEEP_VAL
            and old_manifest.get("merger_version") == merger_version
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
        _download_verified(wanxiang_meta, wanxiang_path)
        print(f"Downloading {qwen_meta['url']} ...", flush=True)
        _download_verified(qwen_meta, qwen_gz_path)

        with gzip.open(qwen_gz_path, "rb") as fin, qwen_path.open("wb") as fout:
            shutil.copyfileobj(fin, fout, length=1024 * 1024)

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
        results = dict(line.rsplit("\t", 1) for line in query_proc.stdout.splitlines())
        if results.keys() != set(SAMPLE_QUERIES) or not any(int(value) >= 0 for value in results.values()):
            raise RuntimeError("产物语法模型样例查询失败")

        out_sha256 = _sha256_file(out_path)
        sums_path = tmp_dir / "SHA256SUMS.txt"
        sums_path.write_bytes(_checksum_text(out_sha256).encode("utf-8"))

        manifest = {
            "output": OUTPUT_GRAM_NAME,
            "sha256": out_sha256,
            "size": out_path.stat().st_size,
            "min_keep_val": MIN_KEEP_VAL,
            "merger_version": merger_version,
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
        files = (out_path, sums_path, manifest_path)
        if assets is None:
            subprocess.run(
                [
                    "gh", "release", "create", RELEASE_TAG,
                    "--repo", repo,
                    "--title", "Hybrid Grammar Model (Wanxiang + Qwen14B)",
                    "--notes", notes,
                    *map(str, files),
                ],
                check=True,
            )
        else:
            _publish_assets(repo, token, assets, files, notes)

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
