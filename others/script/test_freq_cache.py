#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""运行：uv run --no-project --python 3.12 others/script/test_freq_cache.py"""
from __future__ import annotations

import gzip
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True


def _load_module(name: str, filename: str):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


afc = _load_module("apply_freq_cache", "apply_freq_cache.py")
frost = _load_module("sync_frost", "sync_frost.py")


def write_cache(path: Path, items: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for w, wt in sorted(items.items()):
            fh.write(f"{w}\t{wt}\n")


class ApplyFreqCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rime-freq-cache-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache_path = self.root / "others" / "freq_cache.tsv.gz"
        # Sorted weights: [10, 20, 100, 500, 1000] -> index 5//4 = 1 -> fallback_p25 = 20
        write_cache(
            self.cache_path,
            {
                "补魔": 500,
                "钢板": 1000,
                "神格盘": 100,
                "阿碧": 20,
                "旧词": 10,
            },
        )

    def test_load_cache_computes_p25(self) -> None:
        cache, p25 = afc.load_freq_cache(self.cache_path)
        self.assertEqual(cache["补魔"], 500)
        self.assertEqual(p25, 20)

    def test_rejects_incomplete_header_and_required_empty_dictionary(self) -> None:
        target = self.root / "moegirl.dict.yaml"
        truncated = b"---\nname: moegirl\n"
        target.write_bytes(truncated)
        with self.assertRaisesRegex(ValueError, "词库头"):
            afc.apply_cache_to_targets(self.root, self.cache_path, [target.name])
        self.assertEqual(target.read_bytes(), truncated)

        empty = truncated + b"...\n# empty\n"
        target.write_bytes(empty)
        with self.assertRaisesRegex(ValueError, "正文"):
            afc.apply_cache_to_targets(
                self.root, self.cache_path, [target.name], require_entries=True
            )
        stats, _, _ = afc.apply_cache_to_targets(self.root, self.cache_path, [target.name])
        self.assertEqual(stats[0].rows, 0)
        self.assertEqual(target.read_bytes(), empty)

    def test_two_column_dict_hits_and_fallback(self) -> None:
        moegirl = self.root / "cn_dicts_custom" / "moegirl.dict.yaml"
        moegirl.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "---\r\n"
            "name: moegirl\r\n"
            "sort: by_weight\r\n"
            "version: 0.0.1\r\n"
            "...\r\n"
            "补魔\tbu mo\r\n"
            "新角色\txin jue se\r\n"
            "单列词\r\n"
            "# 注释行\tzhu shi\r\n"
        )
        moegirl.write_bytes(content.encode("utf-8"))

        stats, unscored, p25 = afc.apply_cache_to_targets(
            self.root, self.cache_path, ["cn_dicts_custom/moegirl.dict.yaml"]
        )
        self.assertEqual(p25, 20)
        self.assertEqual(unscored, {"新角色"})
        self.assertEqual((stats[0].rows, stats[0].hit, stats[0].kept_positive, stats[0].fallback), (2, 1, 0, 1))
        self.assertTrue(stats[0].changed)

        updated = moegirl.read_bytes().decode("utf-8")
        expected = (
            "---\r\n"
            "name: moegirl\r\n"
            "sort: by_weight\r\n"
            "version: 0.0.1\r\n"
            "...\r\n"
            "补魔\tbu mo\t500\r\n"
            "新角色\txin jue se\t20\r\n"
            "单列词\r\n"
            "# 注释行\tzhu shi\r\n"
        )
        self.assertEqual(updated, expected)

        # Idempotent on second run
        stats2, unscored2, _ = afc.apply_cache_to_targets(
            self.root, self.cache_path, ["cn_dicts_custom/moegirl.dict.yaml"]
        )
        self.assertFalse(stats2[0].changed)
        self.assertEqual(unscored2, set())
        self.assertEqual(stats2[0].kept_positive, 1)

    def test_three_column_zero_and_positive_weights_and_bom(self) -> None:
        game = self.root / "cn_dicts_frost" / "game.dict.yaml"
        frost_base = self.root / "cn_dicts_frost" / "base.dict.yaml"
        game.parent.mkdir(parents=True, exist_ok=True)

        game.write_bytes(
            b"\xef\xbb\xbf"
            + (
                "---\nname: game\nversion: \"1.0\"\nsort: by_weight\n...\n"
                "神格盘\tshen ge pan\t0\n"
                "新装备\txin zhuang bei\t0\t# 备注\n"
                "光刃\tguang ren\t19\n"
            ).encode("utf-8")
        )
        frost_base.write_bytes(
            (
                "---\nname: base\nversion: \"1.0\"\nsort: by_weight\n...\n"
                "# +_+\n"
                "阿碧\ta bi\t46\n"
                "新词条\txin ci tiao\t55\n"
            ).encode("utf-8")
        )

        stats, unscored, _ = afc.apply_cache_to_targets(
            self.root,
            self.cache_path,
            ["cn_dicts_frost/game.dict.yaml", "cn_dicts_frost/base.dict.yaml"],
        )
        self.assertEqual(unscored, {"新装备"})
        self.assertEqual((stats[0].hit, stats[0].kept_positive, stats[0].fallback), (1, 1, 1))
        self.assertEqual((stats[1].hit, stats[1].kept_positive, stats[1].fallback), (1, 1, 0))

        game_raw = game.read_bytes()
        self.assertTrue(game_raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn("神格盘\tshen ge pan\t100\n", game_raw.decode("utf-8-sig"))
        self.assertIn("新装备\txin zhuang bei\t20\t# 备注\n", game_raw.decode("utf-8-sig"))
        self.assertIn("光刃\tguang ren\t19\n", game_raw.decode("utf-8-sig"))

        base_text = frost_base.read_text(encoding="utf-8")
        self.assertIn("阿碧\ta bi\t20\n", base_text)
        self.assertIn("新词条\txin ci tiao\t55\n", base_text)

    def test_compatible_with_sync_frost_parser_and_baseline(self) -> None:
        for rel in frost.BASELINE_FILES:
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            if rel.endswith(("moegirl.dict.yaml", "steel.dict.yaml")):
                p.write_text("---\nname: test\nversion: \"1.0\"\nsort: by_weight\n...\n补魔\tbu mo\n", encoding="utf-8")
            else:
                p.write_text("---\nname: test\nversion: \"1.0\"\nsort: by_weight\n...\n基础词\tji chu ci\t100\n", encoding="utf-8")

        afc.apply_cache_to_targets(
            self.root,
            self.cache_path,
            ["cn_dicts_custom/moegirl.dict.yaml", "cn_dicts_custom/steel.dict.yaml"],
        )
        baseline = frost.load_baseline(self.root)
        self.assertIn("补魔", baseline)
        self.assertIn("基础词", baseline)

        frost_parsed = frost.parse_dictionary(
            (self.root / "cn_dicts_custom" / "moegirl.dict.yaml").read_bytes(),
            "moegirl.dict.yaml",
        )
        self.assertEqual([f for _, f in frost_parsed.records()], [("补魔", "bu mo", "500")])

    def test_cli_writes_github_step_summary(self) -> None:
        target = self.root / "cn_dicts_custom" / "steel.dict.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("---\nname: steel\nversion: \"1.0\"\nsort: by_weight\n...\n钢板\tgang ban\n冷门钢\tleng men gang\n", encoding="utf-8")
        summary_file = self.root / "summary.md"

        with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary_file)}), patch.object(
            sys,
            "argv",
            [
                "apply_freq_cache.py",
                "--rime-path",
                str(self.root),
                "--cache",
                str(self.cache_path),
                "cn_dicts_custom/steel.dict.yaml",
            ],
        ):
            rc = afc.main()
        self.assertEqual(rc, 0)
        summary_text = summary_file.read_text(encoding="utf-8")
        self.assertIn("cn_dicts_custom/steel.dict.yaml", summary_text)
        self.assertIn("uv run others/script/recalibrate_freq.py", summary_text)


if __name__ == "__main__":
    unittest.main()
