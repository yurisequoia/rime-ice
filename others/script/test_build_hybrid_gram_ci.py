"""验证固定 Release 的完整性检查和上传中断恢复。"""

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "build_hybrid_gram_ci", Path(__file__).with_name("build_hybrid_gram_ci.py")
)
assert spec and spec.loader
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def asset(content: bytes, asset_id: int = 1) -> dict:
    return {
        "id": asset_id,
        "state": "uploaded",
        "size": len(content),
        "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
    }


class HybridGramReleaseTests(unittest.TestCase):
    def test_manifest_requires_matching_model_and_checksum_asset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gram = asset(b"model")
            digest = gram["digest"].removeprefix("sha256:")
            manifest = {"output": builder.OUTPUT_GRAM_NAME, "sha256": digest, "size": gram["size"]}
            manifest_bytes = json.dumps(manifest).encode("utf-8")
            folder = Path(tmp)
            (folder / "manifest.json").write_bytes(manifest_bytes)
            assets = {
                builder.OUTPUT_GRAM_NAME: gram,
                "manifest.json": asset(manifest_bytes),
                "SHA256SUMS.txt": asset(builder._checksum_text(digest).encode("utf-8")),
            }
            with patch.object(builder.subprocess, "run"):
                self.assertEqual(builder._verified_manifest("a/b", folder, assets), manifest)
                self.assertIsNone(builder._verified_manifest("a/b", folder, {
                    **assets, builder.OUTPUT_GRAM_NAME: asset(b"older"),
                }))
                self.assertIsNone(builder._verified_manifest("a/b", folder, {
                    **assets, "SHA256SUMS.txt": asset(b"older"),
                }))
                self.assertIsNone(builder._verified_manifest("a/b", folder, {
                    key: value for key, value in assets.items() if key != "SHA256SUMS.txt"
                }))

    def test_failed_staging_keeps_canonical_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / builder.OUTPUT_GRAM_NAME
            model.write_bytes(b"new")
            old = {model.name: asset(b"old")}
            with (
                patch.object(builder.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "gh")) as gh,
                patch.object(builder, "_rename_asset") as rename,
                patch.object(builder, "_delete_asset") as delete,
            ):
                with self.assertRaises(subprocess.CalledProcessError):
                    builder._publish_assets("a/b", None, old, (model,), "notes")
                self.assertEqual(Path(gh.call_args.args[0][-1]).name, f"staged-{model.name}")
                rename.assert_not_called()
                delete.assert_not_called()

    def test_recovers_missing_canonical_asset_on_next_run(self) -> None:
        name = builder.OUTPUT_GRAM_NAME
        backup = asset(b"old")
        remote = {f"previous-{name}": backup, f"staged-{name}": asset(b"new", 2)}
        with patch.object(builder, "_rename_asset") as rename:
            builder._restore_interrupted_promotions("a/b", remote)
        rename.assert_called_once_with("a/b", backup, name)
        self.assertEqual(remote[name], backup)
        self.assertNotIn(f"previous-{name}", remote)

    def test_failed_promotion_restores_old_model_and_can_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / builder.OUTPUT_GRAM_NAME
            model.write_bytes(b"new")
            staged_name = f"staged-{model.name}"
            previous_name = f"previous-{model.name}"
            remote = {model.name: asset(b"old", 1), staged_name: asset(b"new", 2)}

            def rename(_repo, item, name):
                original = next(key for key, value in remote.items() if value == item)
                if original == staged_name and fail:
                    raise subprocess.CalledProcessError(1, "gh api")
                remote[name] = remote.pop(original)

            def delete(_repo, item):
                self.assertEqual(remote.pop(previous_name), item)

            with (
                patch.object(builder, "_get_release_assets", side_effect=lambda *_: remote.copy()),
                patch.object(builder, "_rename_asset", side_effect=rename),
                patch.object(builder, "_delete_asset", side_effect=delete),
                patch.object(builder.subprocess, "run") as gh,
            ):
                fail = True
                with self.assertRaises(subprocess.CalledProcessError):
                    builder._publish_assets("a/b", None, remote.copy(), (model,), "notes")
                self.assertEqual(remote[model.name]["id"], 1)
                gh.assert_not_called()

                fail = False
                builder._publish_assets("a/b", None, remote.copy(), (model,), "notes")
                self.assertEqual(remote, {model.name: asset(b"new", 2)})
                self.assertIn("edit", gh.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
