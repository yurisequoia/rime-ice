# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""用小狼毫的 rime.dll 对照小鹤双拼组句。输入文件每行：小鹤按键=期望文字。"""

import argparse
import csv
import ctypes as C
import itertools
import math
import os
from pathlib import Path
import re
import shutil
import sys
import time

SCHEMA = "double_pinyin_flypy"
Int, Text, Pointer, Session = C.c_int, C.c_char_p, C.c_void_p, C.c_size_t


class Traits(C.Structure):
    _fields_ = [
        ("data_size", Int),
        *[(k, Text) for k in (
            "shared_data_dir", "user_data_dir", "distribution_name",
            "distribution_code_name", "distribution_version", "app_name",
        )],
        ("modules", Pointer),
        ("min_log_level", Int),
        ("log_dir", Text),
        ("prebuilt_data_dir", Text),
        ("staging_dir", Text),
    ]


class Composition(C.Structure):
    _fields_ = [(k, Int) for k in ("length", "cursor_pos", "sel_start", "sel_end")]
    _fields_ += [("preedit", Text)]


class Menu(C.Structure):
    _fields_ = [
        (k, Int) for k in (
            "page_size", "page_no", "is_last_page", "highlighted_candidate_index", "num_candidates",
        )
    ]
    _fields_ += [("candidates", Pointer), ("select_keys", Text)]


class Context(C.Structure):
    _fields_ = [
        ("data_size", Int), ("composition", Composition), ("menu", Menu),
        ("commit_text_preview", Text), ("select_labels", Pointer),
    ]


class Api(C.Structure):
    _fields_ = [("data_size", Int), ("slots", Pointer * 90)]


class Rime:
    def __init__(self, dll_path):
        self.dll_dir = os.add_dll_directory(str(dll_path.parent))
        self.dll = C.CDLL(str(dll_path))
        self.dll.rime_get_api.restype = C.POINTER(Api)
        self.api = self.dll.rime_get_api().contents
        # Slot indices from librime 1.13.1 src/rime_api.h, starting at setup.
        traits_ptr = C.POINTER(Traits)
        context_ptr = C.POINTER(Context)
        config_ptr = C.POINTER(Pointer)
        for name, index, result, args in (
            ("setup", 0, None, (traits_ptr,)),
            ("initialize", 2, None, (traits_ptr,)),
            ("finalize", 3, None, ()),
            ("deployer_initialize", 7, None, (traits_ptr,)),
            ("deploy_config", 11, Int, (Text, Text)),
            ("create_session", 13, Session, ()),
            ("destroy_session", 15, Int, (Session,)),
            ("clear", 20, None, (Session,)),
            ("context", 23, Int, (Session, context_ptr)),
            ("free_context", 24, Int, (context_ptr,)),
            ("set_option", 27, None, (Session, Text, Int)),
            ("select_schema", 34, Int, (Session, Text)),
            ("schema_open", 35, Int, (Text, config_ptr)),
            ("config_close", 37, Int, (config_ptr,)),
            ("config_cstring", 42, Text, (config_ptr, Text)),
            ("version", 72, Text, ()),
            ("set_input", 89, Int, (Session, Text)),
        ):
            slot_end = Api.slots.offset + (index + 1) * C.sizeof(Pointer)
            if slot_end > self.api.data_size + C.sizeof(Int) or not self.api.slots[index]:
                raise RuntimeError(f"这个 rime.dll 缺少所需接口：{name}")
            setattr(self, name, C.CFUNCTYPE(result, *args)(self.api.slots[index]))

    def check_patch(self, settings):
        config = Pointer()
        if not self.schema_open(SCHEMA.encode(), C.byref(config)):
            raise RuntimeError("无法读取测试方案")
        try:
            for key, expected in settings.items():
                actual = self.config_cstring(C.byref(config), key.encode())
                if actual != expected.encode():
                    raise RuntimeError(f"测试补丁未生效：{key} 读到 {actual!r}，预期 {expected!r}")
            language = self.config_cstring(C.byref(config), b"grammar/language")
            if not language:
                raise RuntimeError("测试方案没有配置 grammar/language")
            return language.decode("utf-8")
        finally:
            self.config_close(C.byref(config))

    def decode(self, session, keys):
        self.clear(session)
        ctx = Context()
        ctx.data_size = C.sizeof(Context) - C.sizeof(Int)
        start = time.perf_counter_ns()
        if not self.set_input(session, keys) or not self.context(session, C.byref(ctx)):
            raise RuntimeError(f"读取候选失败：{keys.decode()}")
        elapsed_ms = (time.perf_counter_ns() - start) / 1e6
        try:
            return (ctx.commit_text_preview or b"").decode("utf-8"), elapsed_ms
        finally:
            self.free_context(C.byref(ctx))


def load_samples(path):
    pairs = []
    for no, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        keys, _, expected = line.partition("=")
        keys, expected = keys.strip(), expected.strip()
        if not re.fullmatch(r"[a-z;']+", keys) or not expected:
            raise ValueError(f"{path.name} 第 {no} 行：请写成「小鹤按键=目标文字」，如 nihc=你好")
        pairs.append((keys.encode("ascii"), expected))
    if not pairs:
        raise ValueError("样本文件里没有有效样本")
    return pairs


def make_traits(source, scratch, log_dir):
    traits = Traits()
    traits.data_size = C.sizeof(Traits) - C.sizeof(Int)
    traits.shared_data_dir = str(source).encode("utf-8")
    traits.user_data_dir = str(scratch).encode("utf-8")
    traits.prebuilt_data_dir = str(source / "build").encode("utf-8")
    traits.staging_dir = str(scratch / "build").encode("utf-8")
    traits.app_name = b"rime-grammar-bench"
    traits.log_dir = str(log_dir).encode("utf-8")
    return traits


def run_case(api, source, opencc, out, name, settings, pairs, setup):
    scratch = out / name
    scratch.mkdir()
    patch = "patch:\n" + "".join(f'  "{k}": {v}\n' for k, v in settings.items())
    (scratch / f"{SCHEMA}.custom.yaml").write_text(patch, encoding="utf-8")
    # Lua opens this database relative to the user directory. OpenCC also
    # needs Weasel's bundled s2t assets, since our shared directory is Rime.
    (scratch / "lua").mkdir()
    shutil.copy2(source / "lua" / "lunar.db", scratch / "lua" / "lunar.db")
    (scratch / "opencc").mkdir()
    for asset in ("s2t.json", "STCharacters.ocd2", "STPhrases.ocd2"):
        shutil.copy2(opencc / asset, scratch / "opencc" / asset)
    traits = make_traits(source, scratch, out)
    if setup:
        api.setup(C.byref(traits))
    session = 0
    try:
        api.deployer_initialize(C.byref(traits))
        if not api.deploy_config(f"{SCHEMA}.schema.yaml".encode(), b"schema/version"):
            raise RuntimeError("无法部署测试配置")
        api.initialize(C.byref(traits))
        model = api.check_patch(settings)
        model_file = source / f"{model}.gram"
        if not model_file.is_file():
            raise FileNotFoundError(f"找不到模型：{model_file}")
        if setup:
            print(f"语法模型：{model}", flush=True)
        session = api.create_session()
        if not session or not api.select_schema(session, SCHEMA.encode()):
            raise RuntimeError("无法选用小鹤双拼测试方案")
        api.set_option(session, b"ascii_mode", 0)
        for keys, _ in pairs[: min(20, len(pairs))]:
            api.decode(session, keys)  # Warm the model and dictionary pages.
        return [api.decode(session, keys) for keys, _ in pairs]
    finally:
        if session:
            api.destroy_session(session)
        api.finalize()


def edit_distance(a, b):
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        next_row = [i]
        for j, y in enumerate(b, 1):
            next_row.append(min(row[j] + 1, next_row[-1] + 1, row[j - 1] + (x != y)))
        row = next_row
    return row[-1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples", type=Path, help="UTF-8 文本，每行一个样本")
    parser.add_argument("--homophones", type=int, nargs="+", default=[1, 8],
                        help="要对照的 max_homophones 数字（默认 1 8）")
    parser.add_argument("--penalties", action="store_true",
                        help="固定一个 --homophones，额外对照 12 组搭配与回退参数")
    parser.add_argument("--dll", type=Path, help="手动指定小狼毫 rime.dll")
    parser.add_argument("--model", help="指定用于对照的模型名（默认读取测试方案的 grammar/language）")
    args = parser.parse_args()
    if any(not 1 <= n <= 100 for n in args.homophones):
        parser.error("--homophones 需要在 1 到 100 之间")
    if args.penalties and len(args.homophones) != 1:
        parser.error("--penalties 需要指定一个 --homophones 数值，例如 8")

    source = Path(os.environ["APPDATA"]) / "Rime"
    for artifact in (f"{SCHEMA}.schema.yaml", f"{SCHEMA}.prism.bin", "rime_ice.table.bin"):
        if not (source / "build" / artifact).is_file():
            raise FileNotFoundError(f"找不到部署文件 {artifact}，请先在小狼毫里重新部署。")
    if (source / f"{SCHEMA}.custom.yaml").exists():
        raise RuntimeError("检测到正式小鹤补丁；请保留该文件。此脚本还需要合并它才能公平评测。")
    dll_path = args.dll
    if dll_path is None:
        installation = (source / "installation.yaml").read_text(encoding="utf-8-sig")
        match = re.search(r'^distribution_version:\s*["\']?([\d.]+)', installation, re.M)
        if not match:
            raise RuntimeError("无法确定小狼毫版本，请用 --dll 指定 rime.dll。")
        dll_path = Path(os.environ["ProgramFiles"]) / "Rime" / f"weasel-{match[1]}" / "rime.dll"
    if not dll_path.is_file():
        raise FileNotFoundError(f"找不到 {dll_path}，请用 --dll 指定 rime.dll。")

    samples = args.samples.resolve()
    pairs = load_samples(samples)
    out = samples.parent / "rime-test-results" / (
        time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
    )
    out.mkdir(parents=True)
    cases = [(f"h{n}", {"translator/max_homophones": str(n)})
             for n in dict.fromkeys(args.homophones)]
    if args.penalties:
        width = args.homophones[0]
        for collocation, fallback in itertools.product((-14, -10, -6), (-10, -6, 0, 3)):
            cases.append((f"h{width}_c{collocation}_n{fallback}", {
                "translator/max_homophones": str(width),
                "grammar/collocation_penalty": str(collocation),
                "grammar/non_collocation_penalty": str(fallback),
            }))
    controls = {
        "translator/enable_user_dict": "false",
        "translator/contextual_suggestions": "false",
    }
    if args.model:
        controls["grammar/language"] = args.model
    api = Rime(dll_path.resolve())
    opencc = dll_path.resolve().parent / 'data' / 'opencc'
    print(f"librime {api.version().decode()} · 小鹤双拼 · {len(pairs)} 条样本", flush=True)
    print(f"测试目录：{out}", flush=True)
    results = {}
    expected_chars = sum(len(expected) for _, expected in pairs)
    for name, settings in cases:
        raw = run_case(api, source, opencc, out, name, controls | settings, pairs, not results)
        hits = sum(result == expected for (_, expected), (result, _) in zip(pairs, raw))
        errors = sum(edit_distance(expected, result) for (_, expected), (result, _) in zip(pairs, raw))
        p95 = sorted(ms for _, ms in raw)[math.ceil(0.95 * len(raw)) - 1]
        results[name] = raw
        print(f"{name:19} 首选正确 {hits}/{len(pairs)} ({hits / len(pairs):.1%})"
              f"  字错误率 {errors / expected_chars:.1%}  整串 P95 {p95:.2f} ms", flush=True)

    csv_path = out / "对照结果.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["按键", "目标"] + [
            title for name in results for title in (f"{name} 首选", f"{name} 毫秒")
        ])
        for index, (keys, expected) in enumerate(pairs):
            row = [keys.decode(), expected]
            for data in results.values():
                result, ms = data[index]
                row.extend((result, f"{ms:.3f}"))
            writer.writerow(row)
    print(f"逐条结果：{csv_path}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError) as exc:
        sys.exit(f"错误：{exc}")

