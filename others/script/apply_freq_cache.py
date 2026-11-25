#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""将静态校准词频缓存回填到目标词库，避免上游定时同步覆盖权重。"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import gzip
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True

DEFAULT_CACHE_REL = "others/freq_cache.tsv.gz"
DEFAULT_TARGETS = (
    "cn_dicts_custom/moegirl.dict.yaml",
    "cn_dicts_custom/steel.dict.yaml",
    "cn_dicts_frost/game.dict.yaml",
    "cn_dicts_frost/medication.dict.yaml",
    "cn_dicts_frost/place.dict.yaml",
    "cn_dicts_frost/base.dict.yaml",
    "cn_dicts_frost/ext.dict.yaml",
)


@dataclass
class ApplyStats:
    rel_path: str
    rows: int
    hit: int
    kept_positive: int
    fallback: int
    changed: bool


def load_freq_cache(cache_path: Path) -> tuple[dict[str, int], int]:
    """读取 gzip 压缩的词频缓存表，并返回 (词频映射, P25 兜底权重)。"""
    if not cache_path.is_file():
        raise FileNotFoundError(f"找不到词频缓存文件: {cache_path}")
    cache: dict[str, int] = {}
    with gzip.open(cache_path, "rt", encoding="utf-8") as fh:
        for line in fh:
            s = line.rstrip("\r\n")
            if not s or s.startswith("#"):
                continue
            parts = s.split("\t")
            if len(parts) < 2:
                continue
            word = parts[0].strip()
            if not word:
                continue
            try:
                weight = int(parts[1].strip())
            except ValueError:
                continue
            if weight >= 1:
                cache[word] = weight
    if not cache:
        raise ValueError(f"词频缓存文件为空或无有效词条: {cache_path}")
    sorted_weights = sorted(cache.values())
    fallback_p25 = max(1, sorted_weights[len(sorted_weights) // 4])
    return cache, fallback_p25


def _split_line_ending(raw_line: bytes) -> tuple[bytes, bytes]:
    if raw_line.endswith(b"\r\n"):
        return raw_line[:-2], b"\r\n"
    if raw_line.endswith(b"\n"):
        return raw_line[:-1], b"\n"
    if raw_line.endswith(b"\r"):
        return raw_line[:-1], b"\r"
    return raw_line, b""


def _parse_positive_int(text: str) -> int | None:
    s = text.strip()
    if not s:
        return None
    try:
        val = int(s)
    except ValueError:
        return None
    return val if val > 0 else None


def apply_cache_to_dict(
    filepath: Path,
    rel_path: str,
    cache: dict[str, int],
    fallback_p25: int,
    *,
    dry_run: bool = False,
    require_entries: bool = False,
) -> tuple[ApplyStats, set[str]]:
    """对单个 .dict.yaml 回填校准权重，保留原有换行符、BOM、头部及注释。"""
    raw_bytes = filepath.read_bytes()
    lines = raw_bytes.splitlines(keepends=True)
    out_lines: list[bytes] = []
    header_started = in_body = False
    rows = hit = kept_positive = fallback = 0
    unscored_words: set[str] = set()

    for raw_line in lines:
        if not in_body:
            out_lines.append(raw_line)
            header_line = raw_line.decode("utf-8-sig").strip()
            if header_line == "---":
                header_started = True
            elif header_started and header_line == "...":
                in_body = True
            elif not header_started and header_line and not header_line.startswith("#"):
                raise ValueError(f"{rel_path}: 缺少 Rime 词库头起始标记（---）")
            continue

        body_bytes, ending = _split_line_ending(raw_line)
        line_str = body_bytes.decode("utf-8")
        if not line_str.strip() or line_str.lstrip().startswith("#"):
            out_lines.append(raw_line)
            continue

        fields = line_str.split("\t")
        if len(fields) < 2:
            out_lines.append(raw_line)
            continue

        word = fields[0].strip()
        if not word:
            out_lines.append(raw_line)
            continue

        rows += 1
        new_weight: str | None = None
        if word in cache:
            new_weight = str(cache[word])
            hit += 1
        elif len(fields) >= 3 and _parse_positive_int(fields[2]) is not None:
            kept_positive += 1
            out_lines.append(raw_line)
            continue
        else:
            new_weight = str(fallback_p25)
            fallback += 1
            unscored_words.add(word)

        if len(fields) == 2:
            new_fields = [fields[0], fields[1], new_weight]
        else:
            if fields[2].strip() == new_weight:
                out_lines.append(raw_line)
                continue
            new_fields = list(fields)
            new_fields[2] = new_weight
        out_lines.append(("\t".join(new_fields)).encode("utf-8") + ending)

    if not in_body:
        raise ValueError(f"{rel_path}: 缺少完整的 Rime 词库头（--- / ...）")
    if require_entries and rows == 0:
        raise ValueError(f"{rel_path}: 词库正文没有可回填的词条")

    new_bytes = b"".join(out_lines)
    changed = new_bytes != raw_bytes
    if changed and not dry_run:
        filepath.write_bytes(new_bytes)

    return ApplyStats(
        rel_path=rel_path,
        rows=rows,
        hit=hit,
        kept_positive=kept_positive,
        fallback=fallback,
        changed=changed,
    ), unscored_words


def apply_cache_to_targets(
    rime_path: Path,
    cache_path: Path,
    targets: list[str] | tuple[str, ...] | None = None,
    *,
    dry_run: bool = False,
    require_entries: bool = False,
) -> tuple[list[ApplyStats], set[str], int]:
    """批量对目标词库回填静态缓存词频。"""
    cache, fallback_p25 = load_freq_cache(cache_path)
    target_list = list(targets) if targets else list(DEFAULT_TARGETS)
    stats_list: list[ApplyStats] = []
    all_unscored: set[str] = set()

    for rel in target_list:
        target_path = Path(rel)
        if target_path.is_absolute():
            abs_path = target_path
            rel_label = target_path.name
        else:
            abs_path = rime_path / rel
            rel_label = Path(rel).as_posix()
        if not abs_path.is_file():
            raise FileNotFoundError(f"找不到目标词库文件: {abs_path}")
        st, unscored = apply_cache_to_dict(
            abs_path, rel_label, cache, fallback_p25,
            dry_run=dry_run, require_entries=require_entries,
        )
        stats_list.append(st)
        all_unscored.update(unscored)

    return stats_list, all_unscored, fallback_p25


def format_report(
    stats_list: list[ApplyStats],
    unscored_count: int,
    fallback_p25: int,
    cache_size: int,
) -> str:
    """格式化 Markdown 汇报表格（兼容终端输出与 GITHUB_STEP_SUMMARY）。"""
    lines = [
        f"### 词频缓存回填结果（缓存条目 `{cache_size:,}`，P25 兜底权重 `{fallback_p25}`）",
        "",
        "| File | Rows | Cache Hit | Kept Positive | Fallback (P25) | Changed |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for s in stats_list:
        lines.append(
            f"| `{s.rel_path}` | {s.rows:,} | {s.hit:,} | {s.kept_positive:,} | "
            f"{s.fallback:,} | {s.changed} |"
        )
    lines.append("")
    lines.append(f"- 累计未命中且无正权重的去重词条数：`{unscored_count:,}`")
    if unscored_count > 0:
        lines.append("- 如需增量校准新词，在本地运行：`uv run others/script/recalibrate_freq.py`")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "targets",
        nargs="*",
        help="要回填的词库相对路径（默认全部 7 张自定义与霜冰词库）",
    )
    parser.add_argument(
        "--rime-path",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Rime 仓库根目录（默认自动定位）",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path(DEFAULT_CACHE_REL),
        help="词频缓存 .tsv.gz 路径（默认 others/freq_cache.tsv.gz）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅统计并检查，不写入修改文件",
    )
    parser.add_argument(
        "--require-entries",
        action="store_true",
        help="目标词库没有可回填的词条时退出失败",
    )
    args = parser.parse_args()

    rime_path = args.rime_path.resolve()
    cache_path = args.cache if args.cache.is_absolute() else (rime_path / args.cache)
    cache, _ = load_freq_cache(cache_path)
    stats_list, all_unscored, fallback_p25 = apply_cache_to_targets(
        rime_path, cache_path, args.targets,
        dry_run=args.dry_run, require_entries=args.require_entries,
    )
    report = format_report(stats_list, len(all_unscored), fallback_p25, len(cache))
    print(report, end="")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
