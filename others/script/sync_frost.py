#!/usr/bin/env python3
"""同步霜冰词典，不修改当前输入方案。

本地运行：python -I -B -X utf8 others/script/sync_frost.py
修改下面的两个清单即可调整去重范围；每次都从 master 重新下载原始词典。
"""

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re
import sys
from urllib.error import URLError
from urllib.request import Request, urlopen

SOURCE_URL = "https://raw.githubusercontent.com/gaboolic/rime-frost/master/"
SOURCE_FILES = (
    "cn_dicts/base.dict.yaml",
    "cn_dicts/ext.dict.yaml",
    "cn_dicts/GB18030-2022.dict.yaml",
    "cn_dicts/others.dict.yaml",
    "cn_dicts/corrections.dict.yaml",
    "cn_dicts_cell/game.dict.yaml",
    "cn_dicts_cell/medication.dict.yaml",
    "cn_dicts_cell/place.dict.yaml",
)

# 去重基准：仅加载未注释的词库，不扫描目录，不包含 frost 输出。
BASELINE_FILES = (
    "cn_dicts/8105.dict.yaml",
    "cn_dicts/41448.dict.yaml",
    "cn_dicts/base.dict.yaml",
    "cn_dicts/ext.dict.yaml",
    # "cn_dicts/tencent.dict.yaml",  # 取消注释后将腾讯词库加入去重基准
    "cn_dicts/others.dict.yaml",
    "cn_dicts_custom/custom.dict.yaml",
    "cn_dicts_custom/steel.dict.yaml",
    "cn_dicts_custom/moegirl.dict.yaml",
)

# 参与去重的源文件：取消 corrections 这一行的注释即可对它去重。
# 不在此清单中的源文件仍会下载、校验并完整保留。
DEDUPE_FILES = {
    "cn_dicts/base.dict.yaml",
    "cn_dicts/ext.dict.yaml",
    "cn_dicts/GB18030-2022.dict.yaml",
    "cn_dicts/others.dict.yaml",
    # "cn_dicts/corrections.dict.yaml",  # 取消注释后对 corrections 去重
    "cn_dicts_cell/game.dict.yaml",
    "cn_dicts_cell/medication.dict.yaml",
    "cn_dicts_cell/place.dict.yaml",
}


@dataclass
class Dictionary:
    lines: list[bytes]
    body_start: int
    columns: tuple[str, ...] | None

    def records(self):
        for index in range(self.body_start, len(self.lines)):
            line = self.lines[index].decode("utf-8").rstrip("\r\n")
            if line.strip() and not line.lstrip().startswith("#"):
                fields = tuple(line.split("\t"))
                if not fields[0].strip():
                    raise ValueError(f"line {index + 1}: empty text field")
                yield index, fields


def parse_dictionary(data: bytes, label: str) -> Dictionary:
    """只解析本次使用的 Rime 头部和正文，不依赖 # +_+ 标记。"""
    try:
        data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError(f"{label}: invalid UTF-8") from error
    if b"\x00" in data:
        raise ValueError(f"{label}: unexpected NUL byte")
    lines = data.splitlines(keepends=True)
    start = None
    end = None
    for index, raw in enumerate(lines):
        line = raw.decode("utf-8-sig").strip()
        if start is None:
            if line == "---":
                start = index
            elif line and not line.startswith("#"):
                raise ValueError(f"{label}:{index + 1}: expected Rime header")
        elif line == "...":
            end = index
            break
    if start is None or end is None:
        raise ValueError(f"{label}: missing --- / ... header boundary")

    metadata = {}
    columns = None
    in_columns = False
    for raw in lines[start + 1:end]:
        line = raw.decode("utf-8").strip()
        if not line or line.startswith("#"):
            continue
        if in_columns and line.startswith("- "):
            columns.append(line[2:].strip())
            continue
        match = re.fullmatch(r"([a-z_]+):\s*(.*)", line)
        if not match or match[1] in metadata:
            raise ValueError(f"{label}: unsupported or duplicate header field: {line}")
        key, value = match.groups()
        metadata[key] = value
        in_columns = key == "columns"
        if in_columns:
            if value:
                raise ValueError(f"{label}: expected a block columns list")
            columns = []
    for key in ("name", "version", "sort"):
        if not metadata.get(key, "").strip("\"' "):
            raise ValueError(f"{label}: missing header field {key}")
    if columns is not None and (not columns or columns[0] != "text"):
        raise ValueError(f"{label}: first column must be text")
    return Dictionary(lines, end + 1, tuple(columns) if columns is not None else None)


def load_baseline(rime_path: Path, paths=None) -> set[str]:
    if paths is None:
        paths = BASELINE_FILES
    words = set()
    for relative in paths:
        dictionary = parse_dictionary((rime_path / relative).read_bytes(), relative)
        words.update(fields[0] for _, fields in dictionary.records())
    return words


def source_record(source: str, raw: bytes, fields: tuple[str, ...], line: int):
    """校验源记录；仅修复 game 已出现的空格分列错误。"""
    normalized = False
    if source == "cn_dicts_cell/game.dict.yaml" and len(fields) != 3:
        text = raw.decode("utf-8").rstrip("\r\n")
        match = re.fullmatch(r"([^\s]+)\s+([a-z]+(?:\s+[a-z]+)*)\s+([0-9]+)", text)
        if match:
            fields = (match[1], " ".join(match[2].split()), match[3])
            ending = raw[len(raw.rstrip(b"\r\n")):]
            raw = "\t".join(fields).encode("utf-8") + ending
            normalized = True
    is_gb = source == "cn_dicts/GB18030-2022.dict.yaml"
    is_corrections = source == "cn_dicts/corrections.dict.yaml"
    valid_columns = len(fields) == (4 if is_corrections else 3)
    if is_gb and len(fields) == 4 and fields[3].startswith("#"):
        valid_columns = True
    if not valid_columns or not fields[1].strip() or not re.fullmatch(r"[0-9]+", fields[2]):
        raise ValueError(f"{source}:{line}: invalid text/code/weight columns")
    return raw, fields, normalized


def transform_source(source: str, data: bytes, baseline: set[str], dedupe_files=None):
    if dedupe_files is None:
        dedupe_files = DEDUPE_FILES
    dictionary = parse_dictionary(data, source)
    expected_columns = ("text", "code", "weight")
    if source == "cn_dicts/corrections.dict.yaml":
        expected_columns += ("comment",)
        if dictionary.columns != expected_columns:
            raise ValueError(f"{source}: expected text/code/weight/comment declaration")
    elif dictionary.columns is not None and dictionary.columns != expected_columns:
        raise ValueError(f"{source}: unexpected columns declaration")

    output = dictionary.lines.copy()
    expected_records = []
    count = removed = repaired = 0
    enabled = source in dedupe_files
    for index, fields in dictionary.records():
        raw, fields, normalized = source_record(source, output[index], fields, index + 1)
        count += 1
        repaired += normalized
        if enabled and fields[0] in baseline:
            output[index] = b""
            removed += 1
        else:
            output[index] = raw
            expected_records.append(fields)
    if not count:
        raise ValueError(f"{source}: source dictionary has no records")
    result = b"".join(output)
    actual = parse_dictionary(result, source + " (output)")
    if [fields for _, fields in actual.records()] != expected_records:
        raise ValueError(f"{source}: output does not match filtered source records")
    if enabled and any(fields[0] in baseline for _, fields in actual.records()):
        raise ValueError(f"{source}: output still overlaps the baseline")
    if not enabled and not repaired and result != data:
        raise ValueError(f"{source}: unfiltered output differs from source")
    return result, {
        "source": source,
        "dedupe": enabled,
        "records": count,
        "removed": removed,
        "kept": count - removed,
        "repaired": repaired,
    }


def fetch_source(source: str) -> bytes:
    request = Request(SOURCE_URL + source, headers={"User-Agent": "rime-ice-frost-sync"})
    try:
        with urlopen(request, timeout=60) as response:
            return response.read()
    except (OSError, URLError) as error:
        raise OSError(f"failed to download {SOURCE_URL + source}: {error}") from error


def sync(rime_path: Path):
    baseline = load_baseline(rime_path)
    results = []
    # 全部获取、转换和校验完成后才创建或改写目标文件。
    for source in SOURCE_FILES:
        result, stats = transform_source(source, fetch_source(source), baseline)
        results.append((rime_path / "cn_dicts_frost" / Path(source).name, result, stats))
    for target, result, stats in results:
        stats["changed"] = not target.exists() or target.read_bytes() != result
    for target, result, stats in results:
        if stats["changed"]:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(result)
    return [stats for _, _, stats in results]


def format_report(stats) -> str:
    rows = [
        "Source: https://github.com/gaboolic/rime-frost (master)",
        "",
        "| File | Dedupe | Source rows | Removed | Kept | Repaired separators | Changed |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in stats:
        rows.append(
            f"| {item['source']} | {item['dedupe']} | {item['records']} | "
            f"{item['removed']} | {item['kept']} | {item['repaired']} | {item['changed']} |"
        )
    return "\n".join(rows) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rime-path", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    try:
        report = format_report(sync(args.rime_path.resolve()))
        print(report, end="")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as output:
                output.write(report)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Frost sync failed: {error}\n")


if __name__ == "__main__":
    main()
