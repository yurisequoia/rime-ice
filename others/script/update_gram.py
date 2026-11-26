# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""下载最新融合版 Rime 语法模型并安全替换小狼毫/鼠须管用户目录中的 .gram 文件。"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
from collections.abc import Mapping
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

DEFAULT_REPO = "yurisequoia/rime-ice"
DEFAULT_TAG = "gram"
DEFAULT_GRAM_NAME = "wanxiang-qwen14b-zh-hans.gram"

GRAM_MAGIC = b"Rime::Grammar/"
# marisa/Rime::Grammar 文件头布局（44 字节）:
#   0..31  char[32]  format ("Rime::Grammar/...")
#   32..35 uint32    db_checksum
#   36..39 uint32    double_array_size
#   40..43 int32     array_offset_rel (相对于偏移 40 计算双数组起始位置)
GRAM_HEADER_STRUCT = struct.Struct("<32sIIi")

SQUIRREL_BIN = Path("/Library/Input Methods/Squirrel.app/Contents/MacOS/Squirrel")
SCHEMA_FILES = ("rime_ice.schema.yaml", "double_pinyin_flypy.schema.yaml")
POST_SYNC_DELAY_SEC = 15

WINDOWS_LOCK_WINERRORS = {5, 32, 1224}
LOCK_ERRNOS = {errno.EACCES, errno.EPERM, errno.EBUSY}

_GITHUB_REMOTE_RE = re.compile(
    r"^(?:https?://github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?$"
)
_SHA256_LINE_RE = re.compile(r"^\s*([0-9a-fA-F]{64})\s+[* ]?(.+?)\s*$")


class ReleaseNotFoundError(RuntimeError):
    """目标 GitHub Release 或其语法模型资产尚未发布。"""


def sha256_file(path: Path) -> str:
    """计算文件的小写十六进制 SHA-256 摘要。"""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def validate_gram_file(path: Path) -> tuple[int, int]:
    """校验 Rime .gram 二进制头部与双数组边界，返回 (double_array_size, file_size)。"""
    if not path.is_file():
        raise ValueError(f"语法模型文件不存在: {path}")

    file_size = path.stat().st_size
    if file_size < GRAM_HEADER_STRUCT.size:
        raise ValueError(
            f"语法模型文件过小 ({file_size} 字节 < {GRAM_HEADER_STRUCT.size} 字节): {path}"
        )

    with path.open("rb") as fh:
        header = fh.read(GRAM_HEADER_STRUCT.size)
    if len(header) < GRAM_HEADER_STRUCT.size:
        raise ValueError(f"无法读取完整 .gram 文件头: {path}")

    format_bytes, _checksum, double_array_size, array_offset_rel = (
        GRAM_HEADER_STRUCT.unpack(header)
    )
    if not format_bytes.startswith(GRAM_MAGIC):
        raise ValueError(f"无效的 .gram 魔数头: {format_bytes[:24]!r}")

    array_start = 40 + array_offset_rel
    if array_start < GRAM_HEADER_STRUCT.size:
        raise ValueError(
            f"无效的双数组起始偏移 {array_start} (< {GRAM_HEADER_STRUCT.size})"
        )
    if double_array_size <= 0:
        raise ValueError(f"无效的双数组单元数: {double_array_size}")

    min_size = array_start + double_array_size * 4
    if file_size < min_size:
        raise ValueError(
            f"语法模型文件被截断 ({file_size} < 预期最小 {min_size} 字节)"
        )

    return double_array_size, file_size


def _read_windows_weasel_user_dir() -> Path | None:
    try:
        import winreg
    except ImportError:
        return None

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Rime\Weasel") as key:
            value, _reg_type = winreg.QueryValueEx(key, "RimeUserDir")
    except OSError:
        return None

    if isinstance(value, str) and value.strip():
        expanded = os.path.expandvars(value.strip())
        return Path(expanded).expanduser()
    return None


def resolve_rime_dir(
    explicit: Path | None = None,
    *,
    platform_name: str | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """解析 Rime 用户根目录（支持显式指定、Windows 小狼毫、macOS 鼠须管及 Linux）。"""
    if explicit is not None:
        return explicit.expanduser().resolve()

    plat = platform_name or platform.system()
    environ = os.environ if env is None else env

    if plat == "Windows":
        reg_dir = _read_windows_weasel_user_dir()
        if reg_dir is not None:
            return reg_dir
        appdata = environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "Rime"
        return Path.home() / "AppData" / "Roaming" / "Rime"

    if plat == "Darwin":
        return Path.home() / "Library" / "Rime"

    candidates = (
        Path.home() / ".local" / "share" / "fcitx5" / "rime",
        Path.home() / ".config" / "ibus" / "rime",
        Path.home() / ".config" / "fcitx" / "rime",
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def parse_github_repo_from_remote(remote_url: str | None) -> str | None:
    """从 Git remote URL 中提取 owner/repo 形式的 GitHub 仓库名。"""
    if not remote_url:
        return None
    match = _GITHUB_REMOTE_RE.match(remote_url.strip())
    if not match:
        return None
    owner = match.group("owner")
    repo = match.group("repo")
    if not owner or not repo:
        return None
    return f"{owner}/{repo}"


def detect_github_repo(rime_dir: Path, default: str = DEFAULT_REPO) -> str:
    """尝试从 Rime 目录的 Git origin remote 自动推断 GitHub 仓库名。"""
    if not rime_dir.exists():
        return default
    try:
        proc = subprocess.run(
            ["git", "-C", str(rime_dir), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return default

    if proc.returncode != 0:
        return default
    parsed = parse_github_repo_from_remote(proc.stdout)
    return parsed or default


def _extract_grammar_language(schema_text: str) -> str | None:
    grammar_indent: int | None = None
    for raw_line in schema_text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if grammar_indent is not None:
            if indent <= grammar_indent:
                grammar_indent = None
            elif stripped.startswith("language:"):
                val = stripped.split(":", 1)[1].split("#", 1)[0].strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                return val or None

        if grammar_indent is None and re.match(r"^grammar:\s*(?:#.*)?$", stripped):
            grammar_indent = indent
    return None


def check_schema_language(rime_dir: Path, expected_lang: str) -> list[str]:
    """检查 Rime 目录中方案的 grammar/language 设置，不一致时返回提示信息（不阻断执行）。"""
    warnings: list[str] = []
    for rel_name in SCHEMA_FILES:
        schema_path = rime_dir / rel_name
        if not schema_path.is_file():
            continue
        try:
            text = schema_path.read_text(encoding="utf-8")
        except OSError:
            continue
        actual_lang = _extract_grammar_language(text)
        if actual_lang and actual_lang != expected_lang:
            warnings.append(
                f"{schema_path.name} 当前 grammar/language 为 {actual_lang!r}，"
                f"与目标模型 {expected_lang!r} 不一致，如需启用请同步修改方案配置。"
            )
    return warnings


def _github_headers(token: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "rime-ice-update-gram/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _http_get_bytes(url: str, headers: Mapping[str, str]) -> bytes:
    req = urllib.request.Request(url, headers=dict(headers))
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ReleaseNotFoundError(f"HTTP 404: 未找到资源 {url}") from exc
        raise


def _parse_sha256_sums(text: str, gram_name: str) -> str | None:
    target_base = Path(gram_name).name
    for line in text.splitlines():
        match = _SHA256_LINE_RE.match(line)
        if not match:
            continue
        digest, name = match.group(1).lower(), match.group(2).strip()
        if Path(name).name == target_base:
            return digest
    return None


def fetch_release_info(
    repo: str,
    tag: str,
    gram_name: str,
    token: str | None = None,
) -> dict[str, object]:
    """查询 GitHub Releases API 获取指定 tag 下的语法模型下载链接与预期 SHA-256。"""
    resolved_token = (
        token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    )
    headers = _github_headers(resolved_token)
    api_url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"

    try:
        raw = _http_get_bytes(api_url, headers)
    except ReleaseNotFoundError as exc:
        raise ReleaseNotFoundError(
            f"仓库 {repo} 尚未发布 tag '{tag}' 的 Release。"
        ) from exc

    release = json.loads(raw.decode("utf-8"))
    assets = release.get("assets") or []

    gram_asset: dict | None = None
    manifest_asset: dict | None = None
    sums_asset: dict | None = None
    for asset in assets:
        name = asset.get("name")
        if name == gram_name:
            gram_asset = asset
        elif name == "manifest.json":
            manifest_asset = asset
        elif name == "SHA256SUMS.txt":
            sums_asset = asset

    if gram_asset is None:
        raise ReleaseNotFoundError(
            f"Release '{tag}' 中未找到资产 '{gram_name}'。"
        )

    expected_sha256: str | None = None
    digest_val = gram_asset.get("digest")
    if isinstance(digest_val, str) and digest_val.lower().startswith("sha256:"):
        candidate = digest_val.split(":", 1)[1].strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", candidate):
            expected_sha256 = candidate

    if expected_sha256 is None and manifest_asset is not None:
        manifest_url = manifest_asset.get("browser_download_url")
        if manifest_url:
            manifest_bytes = _http_get_bytes(
                manifest_url, {"User-Agent": "rime-ice-update-gram/1.0"}
            )
            manifest = json.loads(manifest_bytes.decode("utf-8"))
            sha_val = manifest.get("sha256") if manifest.get("output") == gram_name else None
            if isinstance(sha_val, str) and re.fullmatch(
                r"[0-9a-fA-F]{64}", sha_val.strip()
            ):
                expected_sha256 = sha_val.strip().lower()

    if expected_sha256 is None and sums_asset is not None:
        sums_url = sums_asset.get("browser_download_url")
        if sums_url:
            sums_bytes = _http_get_bytes(
                sums_url, {"User-Agent": "rime-ice-update-gram/1.0"}
            )
            expected_sha256 = _parse_sha256_sums(
                sums_bytes.decode("utf-8", errors="replace"), gram_name
            )

    return {
        "tag": release.get("tag_name") or tag,
        "name": gram_name,
        "published_at": release.get("published_at") or "",
        "html_url": release.get("html_url") or "",
        "download_url": gram_asset["browser_download_url"],
        "size": gram_asset.get("size"),
        "sha256": expected_sha256,
    }


def download_file(url: str, dest: Path) -> None:
    """下载文件到目标临时路径；优先使用支持断点续传的 curl。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    curl_bin = shutil.which("curl.exe") or shutil.which("curl")
    if curl_bin:
        cmd = [curl_bin, "-f", "-L", "-C", "-", "--retry", "5", "-o", str(dest), url]
        proc = subprocess.run(cmd, check=False)
        if proc.returncode == 0 and dest.is_file():
            return
        # 若断点续传状态与远端不兼容，清理后直接重新下载一次
        if dest.exists():
            dest.unlink()
        retry_cmd = [curl_bin, "-f", "-L", "--retry", "5", "-o", str(dest), url]
        retry_proc = subprocess.run(retry_cmd, check=False)
        if retry_proc.returncode == 0 and dest.is_file():
            return
        raise RuntimeError(
            f"curl 下载失败 (退出码 {retry_proc.returncode}): {url}"
        )

    req = urllib.request.Request(
        url, headers={"User-Agent": "rime-ice-update-gram/1.0"}
    )
    with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as out:
        shutil.copyfileobj(resp, out, length=1024 * 1024)


def _read_weasel_root_registry() -> Path | None:
    try:
        import winreg
    except ImportError:
        return None

    for subkey in (r"SOFTWARE\WOW6432Node\Rime\Weasel", r"SOFTWARE\Rime\Weasel"):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, subkey) as key:
                value, _reg_type = winreg.QueryValueEx(key, "WeaselRoot")
                if isinstance(value, str) and value.strip():
                    candidate = Path(os.path.expandvars(value.strip()))
                    if (candidate / "WeaselServer.exe").is_file():
                        return candidate
        except OSError:
            continue
    return None


def _parse_yaml_scalar(raw_val: str) -> str:
    text = raw_val.strip()
    if not text:
        return ""
    if text.startswith('"'):
        m = re.match(r'^"((?:[^"\\]|\\.)*)"', text)
        if m:
            return m.group(1).replace(r"\\", "\\").replace(r'\"', '"')
    if text.startswith("'"):
        m = re.match(r"^'((?:[^']|'')*)'", text)
        if m:
            return m.group(1).replace("''", "'")
    return re.split(r"\s+#", text, maxsplit=1)[0].strip()


def _read_installation_fields(rime_dir: Path | None) -> dict[str, str]:
    if rime_dir is None:
        return {}
    inst = rime_dir / "installation.yaml"
    if not inst.is_file():
        return {}
    try:
        text = inst.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return {}

    fields: dict[str, str] = {}
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if raw_line[0].isspace() or ":" not in stripped:
            continue
        key, raw_val = stripped.split(":", 1)
        key = key.strip()
        val = _parse_yaml_scalar(raw_val)
        if key and val:
            fields[key] = val
    return fields


def _read_installation_version(rime_dir: Path | None) -> str | None:
    return _read_installation_fields(rime_dir).get("distribution_version")


def find_weasel_dir(rime_dir: Path | None = None) -> Path | None:
    """定位本机小狼毫（Weasel）程序安装目录。"""
    reg_root = _read_weasel_root_registry()
    if reg_root is not None:
        return reg_root

    bases = []
    for env_key, default in (
        ("ProgramFiles", r"C:\Program Files"),
        ("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ):
        base = Path(os.environ.get(env_key, default)) / "Rime"
        if base not in bases:
            bases.append(base)

    version = _read_installation_version(rime_dir)
    if version:
        for base in bases:
            candidate = base / f"weasel-{version}"
            if (candidate / "WeaselServer.exe").is_file():
                return candidate

    candidates: list[Path] = []
    for base in bases:
        if base.is_dir():
            candidates.extend(
                p
                for p in base.glob("weasel-*")
                if p.is_dir() and (p / "WeaselServer.exe").is_file()
            )
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.name, reverse=True)
    return candidates[0]


def _is_lock_error(exc: OSError) -> bool:
    winerr = getattr(exc, "winerror", None)
    return (winerr in WINDOWS_LOCK_WINERRORS) or (exc.errno in LOCK_ERRNOS)


def _stage_same_dir(staged_src: Path, target_path: Path) -> Path:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.parent / f".{target_path.name}.tmp.{os.getpid()}"
    with staged_src.open("rb") as src_fh, tmp_path.open("wb") as dst_fh:
        shutil.copyfileobj(src_fh, dst_fh, length=1024 * 1024)
        dst_fh.flush()
        os.fsync(dst_fh.fileno())
    return tmp_path


def _retry_os_replace(
    tmp_path: Path,
    target_path: Path,
    *,
    timeout_sec: float,
    interval_sec: float = 0.15,
) -> OSError | None:
    deadline = time.monotonic() + timeout_sec
    last_exc: OSError | None = None
    while True:
        try:
            os.replace(tmp_path, target_path)
            return None
        except OSError as exc:
            if not _is_lock_error(exc):
                raise
            last_exc = exc
            if time.monotonic() >= deadline:
                return last_exc
            time.sleep(interval_sec)


def _start_weasel_server(weasel_dir: Path | None) -> None:
    server_cmd = (
        str(weasel_dir / "WeaselServer.exe") if weasel_dir else "WeaselServer.exe"
    )
    creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    subprocess.Popen(
        [server_cmd],
        cwd=str(weasel_dir) if weasel_dir else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )


def _resolve_weasel_deployer(weasel_dir: Path | None) -> str | None:
    if weasel_dir and (weasel_dir / "WeaselDeployer.exe").is_file():
        return str(weasel_dir / "WeaselDeployer.exe")
    return None


def _wait_after_sync(delay_sec: float = POST_SYNC_DELAY_SEC) -> None:
    if delay_sec <= 0:
        return
    print(f"用户词库同步完成，等待 {delay_sec:g} 秒后再继续部署...")
    time.sleep(delay_sec)


def sync_user_data_windows(
    rime_dir: Path,
    weasel_dir: Path | None = None,
) -> None:
    """在停止服务与重新部署前先调用 WeaselDeployer.exe /sync 同步小狼毫用户词库。"""
    resolved_weasel = weasel_dir or find_weasel_dir(rime_dir)
    deployer = _resolve_weasel_deployer(resolved_weasel)
    if not deployer:
        raise RuntimeError(
            "未找到 WeaselDeployer.exe，无法在部署前同步用户词库；"
            "请手动同步后重试，或使用 --no-sync / --no-deploy。"
        )

    print("正在同步小狼毫用户词库 (WeaselDeployer.exe /sync)...")
    baseline = _local_sync_snapshots(rime_dir)
    proc = subprocess.run(
        [deployer, "/sync"],
        cwd=str(resolved_weasel) if resolved_weasel else None,
        check=False,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"用户词库同步失败 (WeaselDeployer.exe /sync 返回退出码 {proc.returncode})，"
            "已中止后续替换与重新部署以保护用户词库。"
        )
    _require_updated_snapshots(baseline)
    _wait_after_sync()


def _run_weasel_deploy(weasel_dir: Path | None) -> None:
    deployer = _resolve_weasel_deployer(weasel_dir)
    if not deployer:
        raise RuntimeError("未找到 WeaselDeployer.exe，请确认小狼毫已正确安装。")
    proc = subprocess.run(
        [deployer, "/deploy"],
        cwd=str(weasel_dir) if weasel_dir else None,
        check=False,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"WeaselDeployer.exe /deploy 返回退出码 {proc.returncode}。"
        )


def _snapshot_state(path: Path) -> tuple[int, int]:
    try:
        metadata = path.stat()
    except FileNotFoundError:
        return 0, 0
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"用户词库快照不是普通文件: {path}")
    return metadata.st_mtime_ns, metadata.st_size


def _local_sync_snapshots(rime_dir: Path) -> dict[Path, tuple[int, int]]:
    """记录本机各用户词库的导出快照，避免其他设备或配置文件写入误判。"""
    try:
        databases = [path.name for path in rime_dir.iterdir() if path.suffix == ".userdb"]
    except FileNotFoundError:
        return {}
    if not databases:
        return {}
    fields = _read_installation_fields(rime_dir)
    install_id = fields.get("installation_id")
    if not install_id:
        raise RuntimeError("无法定位本机词库快照。请先手动同步，确认安装 ID 后重试。")
    sync_base = Path(os.path.expandvars(fields.get("sync_dir", "sync"))).expanduser()
    sync_target = rime_dir / sync_base / install_id
    snapshots = (sync_target / (name + ".txt") for name in databases)
    return {path: _snapshot_state(path) for path in snapshots}


def _require_updated_snapshots(baseline: dict[Path, tuple[int, int]]) -> None:
    for path, previous in baseline.items():
        current = _snapshot_state(path)
        if current == previous or current[1] == 0:
            raise RuntimeError(
                f"未确认用户词库 {path.name} 的快照已写出。"
                "请检查同步目录或手动同步后使用 --no-sync。"
            )


def wait_for_squirrel_sync(
    baseline: dict[Path, tuple[int, int]],
    *,
    quiet_period: float = 1.2,
    max_wait: float = 30.0,
    poll_interval: float = 0.2,
) -> None:
    """等待每张本机词库的快照均有更新，且写入已稳定。"""
    if not baseline:
        return
    start = time.monotonic()
    latest = baseline
    last_change_at = start

    while True:
        now = time.monotonic()
        current = {path: _snapshot_state(path) for path in baseline}
        if current != latest:
            latest = current
            last_change_at = now

        if now - last_change_at >= quiet_period and all(
            state != baseline[path] and state[1] > 0 for path, state in current.items()
        ):
            return

        if now - start >= max_wait:
            _require_updated_snapshots(baseline)
            raise RuntimeError("用户词库快照写入超时，尚未稳定。")

        time.sleep(poll_interval)


def sync_user_data_posix(
    rime_dir: Path,
    *,
    platform_name: str | None = None,
) -> None:
    """在重新加载前同步 macOS 鼠须管（或尝试同步 Linux Fcitx5 Rime）用户词库。"""
    plat = platform_name or platform.system()
    if plat == "Darwin":
        if not SQUIRREL_BIN.exists():
            raise RuntimeError(
                "未找到 Squirrel 可执行文件，无法在重载前同步用户词库；"
                "请手动同步后重试，或使用 --no-sync / --no-deploy。"
            )
        baseline = _local_sync_snapshots(rime_dir)
        if not baseline:
            print("当前目录没有需要同步的用户词库。")
            return
        print("正在同步鼠须管用户词库 (Squirrel --sync)...")
        proc = subprocess.run([str(SQUIRREL_BIN), "--sync"], check=False, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(
                f"用户词库同步失败 (Squirrel --sync 返回退出码 {proc.returncode})，"
                "已中止后续替换与重新加载。"
            )
        wait_for_squirrel_sync(baseline)
        _wait_after_sync()
        return

    gdbus = shutil.which("gdbus")
    if gdbus:
        print("正在尝试同步 Linux Fcitx5 Rime 用户词库...")
        proc = subprocess.run(
            [
                gdbus,
                "call",
                "--session",
                "--dest",
                "org.fcitx.Fcitx5",
                "--object-path",
                "/rime",
                "--method",
                "org.fcitx.Fcitx.Rime1.SyncUserData",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            print(
                f"提示: 调用 Fcitx5 Rime SyncUserData 未成功（退出码 {proc.returncode}），"
                "将继续完成替换。"
            )
        else:
            _wait_after_sync()
    else:
        print("提示: 当前 Linux 环境未检测到自动同步接口，请按需手动同步用户词库。")


def replace_gram_windows(
    staged_src: Path,
    target_path: Path,
    rime_dir: Path,
    *,
    deploy: bool = True,
    sync: bool = True,
) -> bool:
    """替换 Windows 小狼毫语法模型；部署前先同步用户词库，遇文件锁时自动退出并重启 WeaselServer.exe。"""
    weasel_dir = find_weasel_dir(rime_dir)
    if deploy and sync:
        sync_user_data_windows(rime_dir, weasel_dir)

    tmp_path = _stage_same_dir(staged_src, target_path)
    service_stopped = False

    try:
        try:
            os.replace(tmp_path, target_path)
        except OSError as exc:
            if not deploy or not _is_lock_error(exc):
                raise

            print(
                f"检测到 {target_path.name} 正被小狼毫服务锁定 ({exc})，"
                "正在停止 WeaselServer.exe 以完成替换..."
            )
            server_exe = (
                str(weasel_dir / "WeaselServer.exe")
                if weasel_dir and (weasel_dir / "WeaselServer.exe").is_file()
                else "weaselserver.exe"
            )
            subprocess.run([server_exe, "/q"], check=False, timeout=15)
            service_stopped = True

            last_exc = _retry_os_replace(tmp_path, target_path, timeout_sec=5.0)
            if last_exc is not None:
                print("WeaselServer.exe 仍在占用文件，尝试强制结束进程后重试...")
                subprocess.run(
                    ["taskkill", "/IM", "WeaselServer.exe", "/F"],
                    check=False,
                    capture_output=True,
                    timeout=15,
                )
                last_exc = _retry_os_replace(tmp_path, target_path, timeout_sec=3.0)
                if last_exc is not None:
                    raise last_exc
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        if service_stopped:
            print("正在重启 WeaselServer.exe...")
            _start_weasel_server(weasel_dir)
            time.sleep(0.5)

    if deploy:
        print("正在触发小狼毫重新部署...")
        _run_weasel_deploy(weasel_dir)

    return service_stopped


def replace_gram_posix(
    staged_src: Path,
    target_path: Path,
    rime_dir: Path | None = None,
    *,
    platform_name: str | None = None,
    deploy: bool = True,
    sync: bool = True,
) -> None:
    """在 macOS/Linux 上同目录原子替换 .gram 文件，并在重载前同步用户词库。"""
    resolved_rime_dir = rime_dir or target_path.parent
    plat = platform_name or platform.system()

    if deploy and sync:
        sync_user_data_posix(resolved_rime_dir, platform_name=plat)

    tmp_path = _stage_same_dir(staged_src, target_path)
    try:
        os.replace(tmp_path, target_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

    if not deploy:
        return

    _run_posix_deploy(plat)


def _run_posix_deploy(plat: str) -> None:
    if plat == "Darwin":
        if not SQUIRREL_BIN.exists():
            raise RuntimeError("未找到 Squirrel 可执行文件，请确认鼠须管已正确安装。")
        print("正在触发鼠须管（Squirrel --reload）重新加载...")
        subprocess.run([str(SQUIRREL_BIN), "--reload"], check=True, timeout=60)
        return

    fcitx5_remote = shutil.which("fcitx5-remote")
    if fcitx5_remote:
        subprocess.run([fcitx5_remote, "-r"], check=True, timeout=30)
        return
    ibus = shutil.which("ibus")
    if ibus:
        subprocess.run([ibus, "restart"], check=True, timeout=30)
        return
    raise RuntimeError("未找到 Rime 前端的重载命令，请手动重新部署。")


def _retry_deployment(rime_dir: Path, plat: str, sync: bool) -> None:
    if plat == "Windows":
        weasel_dir = find_weasel_dir(rime_dir)
        if sync:
            sync_user_data_windows(rime_dir, weasel_dir)
        _run_weasel_deploy(weasel_dir)
    else:
        if sync:
            sync_user_data_posix(rime_dir, platform_name=plat)
        _run_posix_deploy(plat)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从 GitHub Release 下载最新融合版 Rime 语法模型并安全替换本地模型。"
    )
    parser.add_argument(
        "--rime-dir",
        type=Path,
        default=None,
        help="指定 Rime 用户根目录（默认自动检测小狼毫/鼠须管配置目录）。",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help=f"GitHub 仓库 owner/repo（默认自动识别 origin，否则 {DEFAULT_REPO}）。",
    )
    parser.add_argument(
        "--tag",
        default=DEFAULT_TAG,
        help=f"Release tag 名称（默认 {DEFAULT_TAG}）。",
    )
    parser.add_argument(
        "--gram-name",
        default=DEFAULT_GRAM_NAME,
        help=f"语法模型文件名（默认 {DEFAULT_GRAM_NAME}）。",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使本地 SHA-256 与远端一致也强制重新下载并覆盖。",
    )
    parser.add_argument(
        "--no-deploy",
        action="store_true",
        help="只替换文件，不同步、停止前端或重新部署。",
    )
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="自动重新部署前跳过用户词库同步（默认在部署前先同步）。",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    plat = platform.system()
    rime_dir = resolve_rime_dir(args.rime_dir, platform_name=plat)
    deploy = not args.no_deploy
    sync = not args.no_sync
    if deploy and args.rime_dir is not None:
        active_dir = resolve_rime_dir(platform_name=plat)
        if rime_dir.resolve() != active_dir.resolve():
            print(f"[错误] 前端使用 {active_dir}。更新其他目录时请加 --no-deploy。", file=sys.stderr)
            return 1
    repo = args.repo or detect_github_repo(rime_dir, default=DEFAULT_REPO)
    target_path = rime_dir / args.gram_name
    pending_deploy = target_path.with_name(target_path.name + ".pending-deploy")
    expected_lang = Path(args.gram_name).stem

    print(f"目标 Rime 目录: {rime_dir}")
    print(f"目标语法模型  : {target_path.name}")
    print(f"Release 来源  : https://github.com/{repo}/releases/tag/{args.tag}")

    for warning in check_schema_language(rime_dir, expected_lang):
        print(f"[警告] {warning}", file=sys.stderr)

    try:
        release = fetch_release_info(repo, args.tag, args.gram_name)
    except ReleaseNotFoundError as exc:
        print(f"\n[提示] {exc}", file=sys.stderr)
        print(
            "请确认已完成 git push 并在 GitHub Actions 运行一次 "
            "'Monthly Build Hybrid Grammar' 工作流以发布首版模型：",
            file=sys.stderr,
        )
        print(
            f"  https://github.com/{repo}/actions/workflows/build_hybrid_gram.yml",
            file=sys.stderr,
        )
        return 1
    except (urllib.error.URLError, json.JSONDecodeError, ValueError) as exc:
        print(f"\n[错误] 查询 GitHub Release 失败: {exc}", file=sys.stderr)
        return 1

    remote_sha256 = release.get("sha256")
    if not isinstance(remote_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", remote_sha256):
        print("[错误] Release 缺少目标模型的 SHA-256，已中止更新。", file=sys.stderr)
        return 1
    if target_path.is_file() and not args.force:
        local_sha256 = sha256_file(target_path)
        if local_sha256 == remote_sha256:
            if deploy and pending_deploy.is_file():
                print("模型文件已是最新，正在重试重新部署...")
                try:
                    _retry_deployment(rime_dir, plat, sync)
                    pending_deploy.unlink(missing_ok=True)
                except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                    print(f"[错误] 重新部署失败: {exc}", file=sys.stderr)
                    return 1
                print("已发出重新部署请求。")
                return 0
            print(
                f"本地语法模型已是最新 (SHA-256: {local_sha256[:16]}...)，无需下载。"
                "如需强制覆盖请添加 --force。"
            )
            return 0

    with tempfile.TemporaryDirectory(prefix="rime_gram_") as tmpdir:
        staged_path = Path(tmpdir) / args.gram_name
        print(f"正在下载: {release['download_url']}")
        try:
            download_file(str(release["download_url"]), staged_path)
        except Exception as exc:
            print(f"\n[错误] 下载语法模型失败: {exc}", file=sys.stderr)
            return 1

        actual_size = staged_path.stat().st_size
        expected_size = release.get("size")
        if isinstance(expected_size, int) and expected_size > 0 and actual_size != expected_size:
            print(
                f"\n[错误] 下载文件大小不匹配: 实际 {actual_size} 字节，预期 {expected_size} 字节。",
                file=sys.stderr,
            )
            return 1

        actual_sha256 = sha256_file(staged_path)
        if actual_sha256 != remote_sha256:
            print(
                "\n[错误] SHA-256 校验失败，已中止替换以保护本地模型：\n"
                f"  预期: {remote_sha256}\n"
                f"  实际: {actual_sha256}",
                file=sys.stderr,
            )
            return 1

        try:
            double_array_size, verified_size = validate_gram_file(staged_path)
        except ValueError as exc:
            print(f"\n[错误] .gram 文件格式校验失败: {exc}", file=sys.stderr)
            return 1

        try:
            if deploy:
                rime_dir.mkdir(parents=True, exist_ok=True)
                pending_deploy.touch()
            if plat == "Windows":
                restarted = replace_gram_windows(
                    staged_path,
                    target_path,
                    rime_dir,
                    deploy=deploy,
                    sync=sync,
                )
                service_msg = (
                    "已自动停止并重启 WeaselServer.exe" if restarted else "直接替换完成"
                )
            else:
                replace_gram_posix(
                    staged_path,
                    target_path,
                    rime_dir,
                    platform_name=plat,
                    deploy=deploy,
                    sync=sync,
                )
                service_msg = "POSIX 原子替换完成"
            if deploy:
                pending_deploy.unlink(missing_ok=True)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            print(f"\n[错误] {exc}", file=sys.stderr)
            return 1

    if not deploy:
        deploy_msg = "已跳过 (--no-deploy)"
    elif sync:
        deploy_msg = "已处理用户词库同步并发出重新部署请求"
    else:
        deploy_msg = "已跳过词库同步并发出重新部署请求 (--no-sync)"

    print("\n更新完成:")
    print(f"  文件路径        : {target_path}")
    print(f"  文件大小        : {verified_size:,} 字节")
    print(f"  Double-Array    : {double_array_size:,} 单元")
    print(f"  SHA-256         : {actual_sha256}")
    print(f"  替换方式        : {service_msg}")
    print(f"  同步与部署      : {deploy_msg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
