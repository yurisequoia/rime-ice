# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""用本仓库的词库和拼写规则，把 UTF-8 中文语料转成「小鹤按键=中文」。"""

import argparse
import csv
from functools import lru_cache
import itertools
from pathlib import Path
import re
import sys
from unicodedata import name

ROOT = Path(__file__).resolve().parents[2]
KEYS_RE = re.compile(r"[a-z;']+")


@lru_cache(maxsize=32768)
def is_han(char):
    return char == "〇" or name(char, "").startswith(
        ("CJK UNIFIED IDEOGRAPH-", "CJK COMPATIBILITY IDEOGRAPH-")
    )


def read_corpus(path):
    segments = []
    mixed_lines = 0
    with path.open(encoding="utf-8-sig") as stream:
        for line_no, line in enumerate(stream, 1):
            keys, _, target = line.partition("=")
            target = target.strip()
            if KEYS_RE.fullmatch(keys.strip()) and target and all(map(is_han, target)):
                raise ValueError(f"第 {line_no} 行好像已有「按键=中文」，可直接用于评测。")
            mixed_lines += any(char.isalnum() and not is_han(char) for char in line)
            for han, chars in itertools.groupby(line, key=is_han):
                if han:
                    segments.append((line_no, "".join(chars)))
    return segments, mixed_lines


def read_flypy_encoder(path):
    rules = []
    lines = iter(path.read_text(encoding="utf-8-sig").splitlines())
    for line in lines:
        if line == "  algebra:":
            break
    for line in lines:
        rule = line.split("#", 1)[0].strip()
        if not rule:
            continue
        if not line.startswith("    - "):
            break
        op, *parts = rule[2:].split("/")
        match op, parts:
            case "derive", [_, _, ""]:
                continue  # Keep the canonical spelling, without optional aliases.
            case "erase", [pattern, ""]:
                rules.append(("erase", re.compile(pattern), None))
            case "xform", [pattern, replacement, ""]:
                replacement = re.sub(r"\$(\d+)", lambda m: rf"\g<{m[1]}>", replacement)
                rules.append(("xform", re.compile(pattern), replacement))
            case "xlit", [before, after, ""]:
                rules.append(("xlit", str.maketrans(before, after), None))
            case _:
                raise ValueError(f"不支持的小鹤拼写规则：{rule}")
    if not rules:
        raise ValueError(f"没有找到小鹤 speller/algebra 规则：{path}")

    @lru_cache(maxsize=4096)
    def encode(syllable):
        if not re.fullmatch("[a-z]+", syllable):
            return None
        for op, pattern, replacement in rules:
            if op == "erase" and pattern.fullmatch(syllable):
                return None
            if op == "xform":
                syllable = pattern.sub(replacement, syllable)
            elif op == "xlit":
                syllable = syllable.translate(pattern)
        return syllable if re.fullmatch("[a-z]+", syllable) else None

    return encode


def dictionary_paths(root):
    entry = root / "rime_ice.dict.yaml"
    yield entry
    lines = iter(entry.read_text(encoding="utf-8-sig").splitlines())
    for line in lines:
        if line == "import_tables:":
            break
    else:
        raise ValueError(f"未找到 import_tables：{entry}")
    for line in lines:
        value = line.split("#", 1)[0].strip()
        if not value:
            continue
        if not value.startswith("- ") or not line[0].isspace():
            break
        table = value[2:].strip()
        if table != "cn_dicts_frost/corrections":
            yield root / f"{table}.dict.yaml"


def load_readings(root, segments, encode):
    single_chars = {char for _, text in segments for char in text}
    prefixes = {text[i:i + 2] for _, text in segments for i in range(len(text) - 1)}
    entries = {}
    for index, path in enumerate(dictionary_paths(root)):
        with path.open(encoding="utf-8-sig") as stream:
            for line in stream:
                if line.strip() == "...":
                    break
            else:
                raise ValueError(f"未找到词典正文分隔符：{path}")
            for line in stream:
                if line.startswith("#"):
                    continue
                term, tab, rest = line.rstrip("\r\n").partition("\t")
                if not tab or not (
                    term in single_chars or len(term) > 1 and term[:2] in prefixes
                ):
                    continue
                spelling, _, weight = rest.partition("\t")
                syllables = spelling.split()
                if len(syllables) != len(term):
                    continue
                keys = [encode(syllable) for syllable in syllables]
                if not all(keys):
                    continue
                code = "".join(keys)
                # Use table priority first. Weights are comparable within a table.
                raw_weight = weight.split("\t", 1)[0].rstrip("%")
                score = (-index, float(raw_weight) if re.fullmatch(r"\d+(?:\.\d+)?", raw_weight) else 0)
                alternatives = entries.setdefault(term, {})
                if code not in alternatives or score > alternatives[code]:
                    alternatives[code] = score
    return {
        term: sorted(codes, key=codes.get, reverse=True)
        for term, codes in entries.items()
    }


def convert(segments, readings):
    max_by_initial = {}
    for word in readings:
        char = word[0]
        max_by_initial[char] = max(max_by_initial.get(char, 0), len(word))

    issues = []
    outputs = []
    for line_no, text in segments:
        codes = []
        unknown = []
        alternatives = []
        i = 0
        while i < len(text):
            limit = min(len(text) - i, max_by_initial.get(text[i], 0))
            for length in range(limit, 0, -1):
                word = text[i:i + length]
                if word in readings:
                    primary, *other = readings[word]
                    codes.append(primary)
                    if other:
                        alternatives.append((word, primary, " ".join(other)))
                    i += length
                    break
            else:
                unknown.append(text[i])
                i += 1
        if unknown:
            for char in dict.fromkeys(unknown):
                issues.append((line_no, text, "无法编码，已跳过整段", char, "", ""))
            continue
        for word, primary, other in alternatives:
            issues.append((line_no, text, "多种读法，请核对", word, primary, other))
        outputs.append(("".join(codes), text))
    return outputs, issues


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="UTF-8 中文 TXT")
    parser.add_argument("-o", "--output", type=Path, help="输出 TXT；默认原文件名加 .flypy.txt")
    args = parser.parse_args()
    output = args.output or args.source.with_suffix(".flypy.txt")
    review = output.with_suffix(".review.tsv")
    for path in (output, review):
        if path.exists():
            raise FileExistsError(f"文件已存在，不覆盖：{path}。请用 -o 指定新文件名。")

    segments, mixed_lines = read_corpus(args.source)
    if not segments:
        raise ValueError("没有可转换的中文片段。")
    encode = read_flypy_encoder(ROOT / "double_pinyin_flypy.schema.yaml")
    rows, issues = convert(segments, load_readings(ROOT, segments, encode))

    if rows:
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.writelines(f"{keys}={text}\n" for keys, text in rows)
        print(f"已生成 {len(rows)} 条：{output.resolve()}")
    if issues:
        with review.open("x", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream, delimiter="\t")
            writer.writerow(["原文行号", "中文片段", "情况", "涉及词字", "已选按键", "其他按键"])
            writer.writerows(issues)
        print(f"核对表（{len(issues)} 项）：{review.resolve()}")
    print(f"因无法编码跳过 {len(segments) - len(rows)} 段。")
    if mixed_lines:
        print(f"{mixed_lines} 行含字母或数字，已在非汉字处切开，只转换中文。")
    if not rows:
        raise ValueError("所有中文片段均无法编码，详见核对表。")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as exc:
        sys.exit(f"错误：{exc}")
