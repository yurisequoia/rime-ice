"""运行：python -I -B -X utf8 others/script/test_sync_frost.py"""

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

# -I 不把脚本目录加入 sys.path；只从明确的本地路径加载被测模块。
spec = importlib.util.spec_from_file_location("sync_frost", Path(__file__).with_name("sync_frost.py"))
frost = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = frost
spec.loader.exec_module(frost)

GB = "cn_dicts/GB18030-2022.dict.yaml"
OTHERS = "cn_dicts/others.dict.yaml"
CORRECTIONS = "cn_dicts/corrections.dict.yaml"
GAME = "cn_dicts_cell/game.dict.yaml"
TENCENT = "cn_dicts/tencent.dict.yaml"


def dictionary(body, *, columns=(), newline="\n"):
    header = ["# Rime dictionary", "---", "name: test", 'version: "1.0"', "sort: by_weight"]
    if columns:
        header += ["columns:", *("  - " + column for column in columns)]
    return (newline.join([*header, "...", body]) + newline).encode("utf-8")


def source_fixture(source):
    if source == CORRECTIONS:
        return dictionary("新词\txin ci\t0\t提示", columns=("text", "code", "weight", "comment"))
    if source == GB:
        return dictionary("新\txin\t0\t# source")
    return dictionary("新词\txin ci\t100")


class TransformTests(unittest.TestCase):
    def test_filters_text_regardless_of_reading_or_weight(self):
        data = dictionary("重复\tchong fu\t100\n重复\tzhong fu\t0\n新词\txin ci\t1")
        result, stats = frost.transform_source(OTHERS, data, {"重复"})
        self.assertEqual(result, dictionary("新词\txin ci\t1"))
        self.assertEqual((stats["records"], stats["removed"], stats["kept"]), (3, 2, 1))

    def test_keeps_internal_and_cross_file_duplicates(self):
        data = dictionary("重庆\tchong qing\t100\n重庆\tzhong qing\t0\n重庆\tchong qing\t100")
        baseline = set()
        for source in (OTHERS, GAME):
            result, stats = frost.transform_source(source, data, baseline)
            self.assertEqual(result, data)
            self.assertEqual(stats["removed"], 0)
        self.assertEqual(baseline, set())

    def test_corrections_is_byte_identical_until_enabled(self):
        data = b"\xef\xbb\xbf" + dictionary(
            "重复\tzhong fu\t0\t正确读音\r\n# 注释",
            columns=("text", "code", "weight", "comment"), newline="\r\n",
        )
        dedupe_files = frost.DEDUPE_FILES - {CORRECTIONS}
        for enabled in (False, True):
            configured = dedupe_files | {CORRECTIONS} if enabled else dedupe_files
            with self.subTest(enabled=enabled), patch.object(frost, "DEDUPE_FILES", configured):
                result, stats = frost.transform_source(CORRECTIONS, data, {"重复"})
                self.assertEqual(stats["dedupe"], enabled)
                if enabled:
                    self.assertNotIn("重复".encode(), result)
                    self.assertIn("# 注释".encode(), result)
                else:
                    self.assertEqual(result, data)

    def test_preserves_gb_fourth_column_and_line_endings(self):
        data = dictionary("字\tzi\t0\t# rime-moran\r\n新\txin\t0\t# Unihan", newline="\r\n")
        result, _ = frost.transform_source(GB, data, {"字"})
        self.assertEqual(result, dictionary("新\txin\t0\t# Unihan", newline="\r\n"))

    def test_repairs_game_separators_before_filtering(self):
        data = dictionary("米提亚 mi ti ya\t15\r\n法尔伽  fa er jia 0\r\n尼可 ni ke 4", newline="\r\n")
        result, stats = frost.transform_source(GAME, data, {"尼可"})
        self.assertEqual(result, dictionary("米提亚\tmi ti ya\t15\r\n法尔伽\tfa er jia\t0", newline="\r\n"))
        self.assertEqual((stats["repaired"], stats["removed"]), (3, 1))
        with self.assertRaises(ValueError):
            frost.transform_source(GAME, dictionary("无法识别的错误行"), set())

    def test_is_deterministic_and_restores_words_from_original_source(self):
        data = dictionary("已有\tyi you\t100\n新增\txin zeng\t100")
        first, _ = frost.transform_source(OTHERS, data, {"已有"})
        second, _ = frost.transform_source(OTHERS, data, {"已有"})
        restored, _ = frost.transform_source(OTHERS, data, set())
        self.assertEqual(first, second)
        self.assertEqual(restored, data)

    def test_all_filtered_is_a_valid_empty_output(self):
        result, stats = frost.transform_source(OTHERS, dictionary("字\tzi\t100"), {"字"})
        self.assertEqual(list(frost.parse_dictionary(result, "output").records()), [])
        self.assertEqual(stats["kept"], 0)

    def test_rejects_bad_headers_encoding_and_source_columns(self):
        cases = (
            b"<html>error</html>", b"\xff", b"---\nname: test\n...\n",
            dictionary("词\tci\tbad-weight"), dictionary("词 ci 100"),
            dictionary("词\t\t100"), dictionary(""),
        )
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValueError):
                frost.transform_source(OTHERS, data, set())
        with self.assertRaises(ValueError):
            frost.transform_source(CORRECTIONS, dictionary("词\tci\t0\t提示"), set())


class BaselineAndSyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rime-frost-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for relative in frost.BASELINE_FILES:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            # 不带 # +_+，兼容一列自动注音和两列显式注音。
            path.write_bytes(dictionary("已有\tyi you\n自动注音\n# 注释词\tzhu shi ci"))

    def test_baseline_ignores_header_comments_and_toggles_tencent(self):
        tencent = self.root / TENCENT
        tencent.write_bytes(dictionary("仅腾讯\t100"))
        baseline_files = tuple(path for path in frost.BASELINE_FILES if path != TENCENT)
        data = dictionary("仅腾讯\tjin teng xun\t100")
        for enabled in (False, True):
            configured = (*baseline_files, TENCENT) if enabled else baseline_files
            with self.subTest(enabled=enabled), patch.object(frost, "BASELINE_FILES", configured):
                baseline = frost.load_baseline(self.root)
                expected = {"已有", "自动注音", "仅腾讯"} if enabled else {"已有", "自动注音"}
                self.assertEqual(baseline, expected)
                result, stats = frost.transform_source(OTHERS, data, baseline)
                if enabled:
                    self.assertEqual(stats["kept"], 0)
                    self.assertNotIn("仅腾讯".encode(), result)
                else:
                    self.assertEqual(result, data)

    def test_disabled_tencent_is_not_opened(self):
        # 文件内容非法也不影响关闭状态，证明没有读取此路径。
        (self.root / TENCENT).write_bytes(b"invalid dictionary")
        baseline_files = tuple(path for path in frost.BASELINE_FILES if path != TENCENT)
        with patch.object(frost, "BASELINE_FILES", baseline_files):
            self.assertEqual(frost.load_baseline(self.root), {"已有", "自动注音"})

    def test_failure_in_last_download_or_validation_leaves_outputs_unchanged(self):
        output = self.root / "cn_dicts_frost"
        output.mkdir()
        for source in frost.SOURCE_FILES:
            (output / Path(source).name).write_bytes(b"original output")
        for failure in ("download", "validation"):
            def fetch(source):
                if source == frost.SOURCE_FILES[-1]:
                    if failure == "download":
                        raise OSError("download interrupted")
                    return b"invalid last dictionary"
                return source_fixture(source)
            with self.subTest(failure=failure), patch.object(frost, "fetch_source", side_effect=fetch):
                with self.assertRaises((OSError, ValueError)):
                    frost.sync(self.root)
            for target in output.iterdir():
                self.assertEqual(target.read_bytes(), b"original output")

    def test_sync_writes_exact_targets_and_skips_unchanged_outputs(self):
        with patch.object(frost, "fetch_source", side_effect=source_fixture):
            stats = frost.sync(self.root)
            self.assertTrue(all(item["changed"] for item in stats))
            paths = list((self.root / "cn_dicts_frost").iterdir())
            self.assertEqual({p.name for p in paths}, {Path(s).name for s in frost.SOURCE_FILES})
            times = {p: p.stat().st_mtime_ns for p in paths}
            stats = frost.sync(self.root)
            self.assertFalse(any(item["changed"] for item in stats))
            self.assertEqual(times, {p: p.stat().st_mtime_ns for p in paths})
            corrections = self.root / "cn_dicts_frost" / Path(CORRECTIONS).name
            self.assertEqual(corrections.read_bytes(), source_fixture(CORRECTIONS))


if __name__ == "__main__":
    unittest.main(verbosity=2)
