# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""others/script/update_gram.py 的单元测试。"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import os
import struct
import subprocess
import tempfile
import unittest
from unittest import mock
import urllib.error

_SCRIPT_PATH = Path(__file__).with_name("update_gram.py")
_SPEC = importlib.util.spec_from_file_location("update_gram", _SCRIPT_PATH)
assert _SPEC and _SPEC.loader
update_gram = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(update_gram)


def _make_fake_gram(
    path: Path,
    *,
    units: int = 8,
    magic: bytes = b"Rime::Grammar/1.0\x00",
    payload_fill: bytes = b"\x01\x02\x03\x04",
    truncate: bool = False,
) -> bytes:
    fmt = magic.ljust(32, b"\x00")[:32]
    header = struct.pack("<32sIIi", fmt, 0x12345678, units, 4)
    body = payload_fill * max(units, 1)
    data = header + body
    if truncate:
        data = data[:-4]
    path.write_bytes(data)
    return data


class UpdateGramTests(unittest.TestCase):
    def test_validate_gram_file_accepts_valid_and_rejects_bad_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid = root / "valid.gram"
            raw = _make_fake_gram(valid, units=12)
            units, size = update_gram.validate_gram_file(valid)
            self.assertEqual(units, 12)
            self.assertEqual(size, len(raw))

            bad_magic = root / "bad_magic.gram"
            _make_fake_gram(bad_magic, units=4, magic=b"NotAGrammar")
            with self.assertRaises(ValueError):
                update_gram.validate_gram_file(bad_magic)

            zero_units = root / "zero_units.gram"
            _make_fake_gram(zero_units, units=0)
            with self.assertRaises(ValueError):
                update_gram.validate_gram_file(zero_units)

            truncated = root / "truncated.gram"
            _make_fake_gram(truncated, units=8, truncate=True)
            with self.assertRaises(ValueError):
                update_gram.validate_gram_file(truncated)

    def test_main_skips_download_when_sha256_matches_and_force_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rime_dir = Path(tmp) / "Rime"
            rime_dir.mkdir()
            target = rime_dir / "wanxiang-qwen14b-zh-hans.gram"
            data = _make_fake_gram(target, units=10)
            digest = hashlib.sha256(data).hexdigest()

            release_info = {
                "tag": "gram",
                "name": target.name,
                "published_at": "2026-11-25T00:00:00Z",
                "html_url": "https://example.invalid/release",
                "download_url": "https://example.invalid/model.gram",
                "size": len(data),
                "sha256": digest,
            }

            with (
                mock.patch.object(
                    update_gram, "fetch_release_info", return_value=release_info
                ),
                mock.patch.object(update_gram, "download_file") as mock_dl,
            ):
                rc = update_gram.main(["--rime-dir", str(rime_dir), "--no-deploy"])
                self.assertEqual(rc, 0)
                mock_dl.assert_not_called()

            def fake_download(_url: str, dest: Path) -> None:
                dest.write_bytes(data)

            with (
                mock.patch.object(
                    update_gram, "fetch_release_info", return_value=release_info
                ),
                mock.patch.object(
                    update_gram, "download_file", side_effect=fake_download
                ) as mock_dl,
                mock.patch.object(update_gram.platform, "system", return_value="Darwin"),
                mock.patch.object(update_gram, "replace_gram_posix") as mock_posix,
            ):
                rc = update_gram.main(
                    ["--rime-dir", str(rime_dir), "--force", "--no-deploy"]
                )
                self.assertEqual(rc, 0)
                mock_dl.assert_called_once()
                mock_posix.assert_called_once()

    def test_main_handles_404_release_gracefully_without_modifying_local(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rime_dir = Path(tmp) / "Rime"
            rime_dir.mkdir()
            target = rime_dir / "wanxiang-qwen14b-zh-hans.gram"
            original = _make_fake_gram(target, units=6)

            err_404 = urllib.error.HTTPError(
                url="https://api.github.com/repos/yurisequoia/rime-ice/releases/tags/gram",
                code=404,
                msg="Not Found",
                hdrs=None,
                fp=io.BytesIO(b""),
            )
            stderr_buf = io.StringIO()
            with (
                mock.patch.object(
                    update_gram.urllib.request, "urlopen", side_effect=err_404
                ),
                mock.patch.object(sys, "stderr", stderr_buf),
            ):
                rc = update_gram.main(
                    ["--rime-dir", str(rime_dir), "--repo", "yurisequoia/rime-ice"]
                )
            self.assertEqual(rc, 1)
            self.assertIn("Monthly Build Hybrid Grammar", stderr_buf.getvalue())
            self.assertEqual(target.read_bytes(), original)

    def test_windows_lock_recovery_stops_replaces_restarts_and_deploys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rime_dir = root / "Rime"
            rime_dir.mkdir()
            weasel_dir = root / "weasel-0.17.4"
            weasel_dir.mkdir()
            (weasel_dir / "WeaselServer.exe").write_bytes(b"MZ")
            (weasel_dir / "WeaselDeployer.exe").write_bytes(b"MZ")

            staged = root / "staged.gram"
            new_bytes = _make_fake_gram(staged, units=14, payload_fill=b"\x09\x08\x07\x06")
            target = rime_dir / "wanxiang-qwen14b-zh-hans.gram"
            _make_fake_gram(target, units=6, payload_fill=b"\x01\x01\x01\x01")

            events: list[str] = []
            real_replace = update_gram.os.replace
            replace_attempts = 0
            lock_err = PermissionError(13, "文件被用户映射区域锁定")
            lock_err.winerror = 1224  # type: ignore[attr-defined]

            def flaky_replace(src: Path, dst: Path) -> None:
                nonlocal replace_attempts
                replace_attempts += 1
                events.append(f"replace:{replace_attempts}")
                if replace_attempts <= 2:
                    raise lock_err
                real_replace(src, dst)

            run_calls: list[list[str]] = []
            popen_calls: list[list[str]] = []

            def fake_run(cmd, **_kwargs):
                call = [str(c) for c in cmd]
                run_calls.append(call)
                events.append(f"run:{Path(call[0]).name}:{' '.join(call[1:])}")
                return subprocess.CompletedProcess(cmd, 0)

            def fake_popen(cmd, **_kwargs):
                call = [str(c) for c in cmd]
                popen_calls.append(call)
                events.append(f"popen:{Path(call[0]).name}")
                return mock.MagicMock()

            with (
                mock.patch.object(
                    update_gram, "find_weasel_dir", return_value=weasel_dir
                ),
                mock.patch.object(
                    update_gram.os, "replace", side_effect=flaky_replace
                ),
                mock.patch.object(update_gram.subprocess, "run", side_effect=fake_run),
                mock.patch.object(
                    update_gram.subprocess, "Popen", side_effect=fake_popen
                ),
                mock.patch.object(update_gram.time, "sleep"),
            ):
                restarted = update_gram.replace_gram_windows(
                    staged, target, rime_dir, deploy=True, sync=True
                )

            self.assertTrue(restarted)
            self.assertEqual(replace_attempts, 3)
            self.assertEqual(target.read_bytes(), new_bytes)
            self.assertEqual(
                run_calls,
                [
                    [str(weasel_dir / "WeaselDeployer.exe"), "/sync"],
                    [str(weasel_dir / "WeaselServer.exe"), "/q"],
                    [str(weasel_dir / "WeaselDeployer.exe"), "/deploy"],
                ],
            )
            self.assertEqual(
                popen_calls, [[str(weasel_dir / "WeaselServer.exe")]]
            )
            self.assertEqual(
                events,
                [
                    "run:WeaselDeployer.exe:/sync",
                    "replace:1",
                    "run:WeaselServer.exe:/q",
                    "replace:2",
                    "replace:3",
                    "popen:WeaselServer.exe",
                    "run:WeaselDeployer.exe:/deploy",
                ],
            )

    def test_windows_sync_failure_aborts_before_replacing_or_deploying(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rime_dir = root / "Rime"
            rime_dir.mkdir()
            weasel_dir = root / "weasel-0.17.4"
            weasel_dir.mkdir()
            (weasel_dir / "WeaselServer.exe").write_bytes(b"MZ")
            (weasel_dir / "WeaselDeployer.exe").write_bytes(b"MZ")

            target = rime_dir / "wanxiang-qwen14b-zh-hans.gram"
            old_bytes = _make_fake_gram(target, units=5, payload_fill=b"OLD!")
            new_bytes = _make_fake_gram(root / "remote.gram", units=9, payload_fill=b"NEW!")

            release_info = {
                "tag": "gram",
                "name": target.name,
                "published_at": "2026-11-25T00:00:00Z",
                "html_url": "https://example.invalid/release",
                "download_url": "https://example.invalid/model.gram",
                "size": len(new_bytes),
                "sha256": hashlib.sha256(new_bytes).hexdigest(),
            }

            run_calls: list[list[str]] = []

            def failing_sync_run(cmd, **_kwargs):
                call = [str(c) for c in cmd]
                run_calls.append(call)
                return subprocess.CompletedProcess(cmd, 2)

            stderr_buf = io.StringIO()
            with (
                mock.patch.object(
                    update_gram, "fetch_release_info", return_value=release_info
                ),
                mock.patch.object(
                    update_gram,
                    "download_file",
                    side_effect=lambda _u, dest: dest.write_bytes(new_bytes),
                ),
                mock.patch.object(update_gram.platform, "system", return_value="Windows"),
                mock.patch.object(
                    update_gram, "find_weasel_dir", return_value=weasel_dir
                ),
                mock.patch.object(
                    update_gram.subprocess, "run", side_effect=failing_sync_run
                ),
                mock.patch.object(update_gram.os, "replace") as mock_replace,
                mock.patch.object(sys, "stderr", stderr_buf),
            ):
                rc = update_gram.main(
                    ["--rime-dir", str(rime_dir), "--repo", "yurisequoia/rime-ice"]
                )

            self.assertEqual(rc, 1)
            self.assertIn("用户词库同步失败", stderr_buf.getvalue())
            self.assertEqual(target.read_bytes(), old_bytes)
            mock_replace.assert_not_called()
            self.assertEqual(
                run_calls, [[str(weasel_dir / "WeaselDeployer.exe"), "/sync"]]
            )

    def test_no_sync_and_no_deploy_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rime_dir = root / "Rime"
            rime_dir.mkdir()
            weasel_dir = root / "weasel-0.17.4"
            weasel_dir.mkdir()
            (weasel_dir / "WeaselServer.exe").write_bytes(b"MZ")
            (weasel_dir / "WeaselDeployer.exe").write_bytes(b"MZ")

            target = rime_dir / "wanxiang-qwen14b-zh-hans.gram"
            _make_fake_gram(target, units=4, payload_fill=b"INIT")
            remote_bytes = _make_fake_gram(root / "r.gram", units=8, payload_fill=b"NEXT")
            release_info = {
                "tag": "gram",
                "name": target.name,
                "published_at": "2026-11-25T00:00:00Z",
                "html_url": "https://example.invalid/release",
                "download_url": "https://example.invalid/model.gram",
                "size": len(remote_bytes),
                "sha256": hashlib.sha256(remote_bytes).hexdigest(),
            }

            run_calls: list[list[str]] = []

            def fake_run(cmd, **_kwargs):
                run_calls.append([str(c) for c in cmd])
                return subprocess.CompletedProcess(cmd, 0)

            with (
                mock.patch.object(
                    update_gram, "fetch_release_info", return_value=release_info
                ),
                mock.patch.object(
                    update_gram,
                    "download_file",
                    side_effect=lambda _u, dest: dest.write_bytes(remote_bytes),
                ),
                mock.patch.object(update_gram.platform, "system", return_value="Windows"),
                mock.patch.object(
                    update_gram, "find_weasel_dir", return_value=weasel_dir
                ),
                mock.patch.object(update_gram.subprocess, "run", side_effect=fake_run),
            ):
                rc_no_sync = update_gram.main(
                    [
                        "--rime-dir",
                        str(rime_dir),
                        "--repo",
                        "yurisequoia/rime-ice",
                        "--force",
                        "--no-sync",
                    ]
                )
                self.assertEqual(rc_no_sync, 0)
                self.assertEqual(
                    run_calls, [[str(weasel_dir / "WeaselDeployer.exe"), "/deploy"]]
                )

                run_calls.clear()
                rc_no_deploy = update_gram.main(
                    [
                        "--rime-dir",
                        str(rime_dir),
                        "--repo",
                        "yurisequoia/rime-ice",
                        "--force",
                        "--no-deploy",
                    ]
                )
                self.assertEqual(rc_no_deploy, 0)
                self.assertEqual(run_calls, [])

    def test_posix_atomic_replace_stages_same_dir_syncs_and_reloads_squirrel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rime_dir = root / "Rime"
            rime_dir.mkdir()
            fake_squirrel = root / "Squirrel"
            fake_squirrel.write_bytes(b"#!/bin/sh\n")

            target = rime_dir / "wanxiang-qwen14b-zh-hans.gram"
            old_bytes = _make_fake_gram(target, units=5, payload_fill=b"OLD!")
            staged = root / "new.gram"
            new_bytes = _make_fake_gram(staged, units=9, payload_fill=b"NEW!")

            events: list[str] = []
            run_calls: list[list[str]] = []
            replace_calls: list[tuple[Path, Path]] = []
            real_replace = update_gram.os.replace

            def record_replace(src: Path, dst: Path) -> None:
                replace_calls.append((Path(src), Path(dst)))
                events.append("replace")
                real_replace(src, dst)

            def fake_run(cmd, **_kwargs):
                call = [str(c) for c in cmd]
                run_calls.append(call)
                events.append(f"run:{' '.join(call[1:])}")
                return subprocess.CompletedProcess(cmd, 0)

            def fake_wait(_rime_dir: Path, _baseline: int) -> None:
                events.append("wait_sync")

            with (
                mock.patch.object(update_gram, "SQUIRREL_BIN", fake_squirrel),
                mock.patch.object(
                    update_gram, "wait_for_squirrel_sync", side_effect=fake_wait
                ),
                mock.patch.object(update_gram.os, "replace", side_effect=record_replace),
                mock.patch.object(update_gram.subprocess, "run", side_effect=fake_run),
            ):
                if os.name == "posix":
                    with open(target, "rb") as open_old_fh:
                        update_gram.replace_gram_posix(
                            staged, target, rime_dir, platform_name="Darwin", deploy=True, sync=True
                        )
                        # POSIX 上已打开描述符应保留旧 inode 数据，直到 Squirrel --reload 切换
                        self.assertEqual(open_old_fh.read(), old_bytes)
                else:
                    update_gram.replace_gram_posix(
                        staged, target, rime_dir, platform_name="Darwin", deploy=True, sync=True
                    )

            self.assertEqual(len(replace_calls), 1)
            src_tmp, dst_path = replace_calls[0]
            self.assertEqual(src_tmp.parent, target.parent)
            self.assertEqual(dst_path, target)
            self.assertEqual([p.name for p in rime_dir.iterdir()], [target.name])
            self.assertEqual(target.read_bytes(), new_bytes)
            self.assertEqual(
                run_calls,
                [[str(fake_squirrel), "--sync"], [str(fake_squirrel), "--reload"]],
            )
            self.assertEqual(
                events, ["run:--sync", "wait_sync", "replace", "run:--reload"]
            )

    def test_wait_for_squirrel_sync_waits_for_sync_dir_write_and_quiet_period(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rime_dir = root / "Rime"
            rime_dir.mkdir()
            sync_root = root / "CloudSync"
            snapshot_dir = sync_root / "macbook"
            snapshot_dir.mkdir(parents=True)

            # 双引号路径内含空格，验证 YAML 标量解析与 sync_dir/installation_id 定位
            (rime_dir / "installation.yaml").write_text(
                f'installation_id: "macbook"\nsync_dir: "{sync_root}"\n',
                encoding="utf-8",
            )
            userdb_txt = snapshot_dir / "rime_ice.userdb.txt"
            userdb_txt.write_text("# header\n", encoding="utf-8")

            baseline = update_gram._latest_sync_mtime_ns(rime_dir)
            fake_clock = [0.0]
            polls = 0

            def fake_monotonic() -> float:
                return fake_clock[0]

            def fake_sleep(dt: float) -> None:
                nonlocal polls
                polls += 1
                fake_clock[0] += dt
                # 在第二次轮询时模拟后台同步写出用户词库快照文件
                if polls == 2:
                    bumped = baseline + 1_000_000_000
                    os.utime(userdb_txt, ns=(bumped, bumped))

            with (
                mock.patch.object(update_gram.time, "monotonic", side_effect=fake_monotonic),
                mock.patch.object(update_gram.time, "sleep", side_effect=fake_sleep),
            ):
                update_gram.wait_for_squirrel_sync(
                    rime_dir,
                    baseline,
                    start_timeout=2.0,
                    quiet_period=0.5,
                    max_wait=10.0,
                    poll_interval=0.1,
                )

            # 0.2s 检测到写入，随后至少静默 0.5s 才返回（总耗时约 0.7s+）
            self.assertGreaterEqual(polls, 7)
            self.assertGreaterEqual(fake_clock[0], 0.7)

    def test_detect_github_repo_and_schema_language_warning(self) -> None:
        self.assertEqual(
            update_gram.parse_github_repo_from_remote(
                "https://github.com/yurisequoia/rime-ice.git"
            ),
            "yurisequoia/rime-ice",
        )
        self.assertEqual(
            update_gram.parse_github_repo_from_remote(
                "git@github.com:foo/bar.git"
            ),
            "foo/bar",
        )
        with tempfile.TemporaryDirectory() as tmp:
            rime_dir = Path(tmp)
            (rime_dir / "rime_ice.schema.yaml").write_text(
                "schema:\n  schema_id: rime_ice\ngrammar:\n  language: wanxiang-lts-zh-hans\n",
                encoding="utf-8",
            )
            warnings = update_gram.check_schema_language(
                rime_dir, "wanxiang-qwen14b-zh-hans"
            )
            self.assertEqual(len(warnings), 1)
            self.assertIn("wanxiang-lts-zh-hans", warnings[0])

    def test_fetch_release_info_parses_sha256_fallback_from_manifest(self) -> None:
        release_payload = json.dumps(
            {
                "tag_name": "gram",
                "published_at": "2026-11-25T00:00:00Z",
                "html_url": "https://example.invalid/gram",
                "assets": [
                    {
                        "name": "wanxiang-qwen14b-zh-hans.gram",
                        "size": 1234,
                        "browser_download_url": "https://example.invalid/gram.bin",
                    },
                    {
                        "name": "manifest.json",
                        "browser_download_url": "https://example.invalid/manifest.json",
                    },
                ],
            }
        ).encode("utf-8")
        manifest_payload = json.dumps(
            {"output": "wanxiang-qwen14b-zh-hans.gram", "sha256": "a" * 64}
        ).encode("utf-8")

        def fake_get(url: str, _headers):
            if url.endswith("/releases/tags/gram"):
                return release_payload
            if url.endswith("/manifest.json"):
                return manifest_payload
            raise AssertionError(f"unexpected url: {url}")

        with mock.patch.object(update_gram, "_http_get_bytes", side_effect=fake_get):
            info = update_gram.fetch_release_info(
                "yurisequoia/rime-ice", "gram", "wanxiang-qwen14b-zh-hans.gram"
            )
        self.assertEqual(info["sha256"], "a" * 64)
        self.assertEqual(info["size"], 1234)


if __name__ == "__main__":
    unittest.main()
