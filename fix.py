#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix-volcengine-glm — 一键修复 Hermes Desktop 看不到火山引擎 coding plan 模型的问题

问题：Hermes Desktop 的模型下拉来自 /api/model/options，对有 api_key 的 custom
provider 默认会调 live /v1/models 覆盖你在 config.yaml 写死的 models。
火山 ark coding/v3 的 /models 返回了一堆 doubao/qwen/deepseek，但没有 glm-5.2、
kimi-k2.6 这些常用别名 —— 实际可用，列表里没有。

解法：在 provider 配置里加 discover_models: false，并保证 models: 是纯字符串
列表（而不是 {id, context_length} 的 dict 列表，那种格式会让 inventory 崩）。

使用：
    python3 fix.py            # 自动修复，含确认
    python3 fix.py --yes      # 自动修复，不问
    python3 fix.py --dry-run  # 只看会改什么，不动文件
    python3 fix.py --rollback # 从最近的 .bak 还原
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import subprocess
import sys
from pathlib import Path

CONFIG = Path.home() / ".hermes" / "config.yaml"
ARK_HINTS = ("ark.cn-beijing.volces.com", "ark.volces.com", "/api/coding/v")

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
BOLD = "\033[1m"
DIM = "\033[2m"
RST = "\033[0m"


def c(text: str, color: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"{color}{text}{RST}"


def info(msg: str) -> None:
    print(f"{c('▸', BOLD)} {msg}")


def ok(msg: str) -> None:
    print(f"{c('✓', GREEN)} {msg}")


def warn(msg: str) -> None:
    print(f"{c('⚠', YELLOW)} {msg}")


def err(msg: str) -> None:
    print(f"{c('✗', RED)} {msg}", file=sys.stderr)


def have_yaml() -> bool:
    try:
        import yaml  # noqa: F401
        return True
    except ImportError:
        return False


def load_yaml(path: Path):
    """Load YAML if PyYAML is available, else return None."""
    if not have_yaml():
        return None
    import yaml
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def find_volcengine_provider(cfg: dict) -> tuple[str, dict] | None:
    """Find the first providers: entry that points at ark.volces / coding/v*.

    Returns (slug, provider_dict) or None.
    """
    providers = cfg.get("providers") or {}
    if not isinstance(providers, dict):
        return None
    for slug, pcfg in providers.items():
        if not isinstance(pcfg, dict):
            continue
        url = str(pcfg.get("base_url") or pcfg.get("api") or pcfg.get("url") or "").lower()
        if any(h in url for h in ARK_HINTS):
            return slug, pcfg
    return None


def diagnose(cfg: dict) -> dict:
    """Return a status dict describing what's wrong (or right)."""
    found = find_volcengine_provider(cfg)
    if not found:
        return {"state": "no_volcengine"}
    slug, pcfg = found
    models = pcfg.get("models")
    discover = pcfg.get("discover_models", True)
    # detect dict-style models entries (the form that crashes inventory.py)
    dict_style = isinstance(models, list) and any(isinstance(m, dict) for m in models)
    string_models: list[str] = []
    if isinstance(models, list):
        for m in models:
            if isinstance(m, str):
                string_models.append(m)
            elif isinstance(m, dict) and m.get("id"):
                string_models.append(str(m["id"]))
    elif isinstance(models, dict):
        string_models = [str(k) for k in models.keys() if k]
    default_model = pcfg.get("model") or pcfg.get("default_model") or ""
    # Detect auxiliary tasks still pointing at 'auto' — these blow up at
    # runtime because vision auto resolves main_provider='custom' (the
    # runtime override for named custom providers) and then loses the
    # named-provider base_url, falling through to openrouter/nous which
    # aren't configured.  See README for the full chain.
    aux_tasks_auto = _diagnose_auxiliary_tasks(cfg)
    return {
        "state": "found",
        "slug": slug,
        "discover_models": discover,
        "dict_style_models": dict_style,
        "models_list": string_models,
        "default_model": default_model,
        "aux_tasks_auto": aux_tasks_auto,
    }


# Auxiliary tasks that default to 'auto' and routinely break for named-custom
# main providers.  Pinning each one to the main provider/model is the
# documented workaround.  Order matters for the user-visible report.
AUX_TASKS_TO_PIN = (
    "vision",
    "web_extract",
    "compression",
    "title_generation",
    "tts_audio_tags",
)


def _diagnose_auxiliary_tasks(cfg: dict) -> list[str]:
    """Return the auxiliary task names that are still on 'auto'."""
    aux = cfg.get("auxiliary") or {}
    if not isinstance(aux, dict):
        return []
    needs_fix: list[str] = []
    for task in AUX_TASKS_TO_PIN:
        tcfg = aux.get(task)
        if not isinstance(tcfg, dict):
            continue
        prov = str(tcfg.get("provider") or "").strip().lower()
        if prov in {"", "auto"}:
            needs_fix.append(task)
    return needs_fix


def _provider_block_span(text: str, slug: str) -> tuple[int, int] | None:
    """Find the [start_line, end_line) line range of a `providers:` entry.

    Heuristic: find a line `  <slug>:` (2-space indent, common in Hermes
    config), then keep going until we hit a line with indent <= 2 that isn't
    blank/comment.  Returns (start, end) line indices into text.splitlines().
    """
    lines = text.splitlines(keepends=True)
    # locate `providers:` first to anchor
    p_idx = None
    for i, ln in enumerate(lines):
        if ln.rstrip() == "providers:" or ln.lstrip().startswith("providers:"):
            p_idx = i
            break
    if p_idx is None:
        return None
    # search for `  <slug>:` after providers:
    target = f"  {slug}:"
    start = None
    for i in range(p_idx + 1, len(lines)):
        if lines[i].rstrip("\n") == target or lines[i].startswith(target + " ") or lines[i].rstrip() == target:
            start = i
            break
    if start is None:
        return None
    # End = next line with indent <= 2 (i.e. a sibling provider or a top-level
    # key).  Comments at indent <= 2 also count as block boundaries — if a
    # comment "belongs" to the next provider it must not be sucked into ours.
    end = len(lines)
    for j in range(start + 1, len(lines)):
        ln = lines[j]
        if not ln.strip():
            continue
        stripped = ln.lstrip(" ")
        indent = len(ln) - len(stripped)
        if indent <= 2:
            end = j
            break
    return (start, end)


def _auxiliary_task_span(text: str, task: str) -> tuple[int, int] | None:
    """Find the [start, end) line range of an ``auxiliary.<task>`` block.

    auxiliary blocks live at 2-space indent under ``auxiliary:`` (top-level).
    The task block ends at the next 2-space-indented sibling or any indent
    <= 2 line.
    """
    lines = text.splitlines(keepends=True)
    aux_idx = None
    for i, ln in enumerate(lines):
        if ln.rstrip() == "auxiliary:" or ln.lstrip().startswith("auxiliary:"):
            aux_idx = i
            break
    if aux_idx is None:
        return None
    target = f"  {task}:"
    start = None
    for i in range(aux_idx + 1, len(lines)):
        ln = lines[i]
        if not ln.strip():
            continue
        stripped = ln.lstrip(" ")
        indent = len(ln) - len(stripped)
        # Once we leave the auxiliary: section, stop.
        if indent <= 0 and stripped and not stripped.startswith("#"):
            return None
        if ln.rstrip("\n") == target or ln.startswith(target + " ") or ln.rstrip() == target:
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        ln = lines[j]
        if not ln.strip():
            continue
        stripped = ln.lstrip(" ")
        indent = len(ln) - len(stripped)
        if indent <= 2:
            end = j
            break
    return (start, end)


def rewrite_auxiliary_task(text: str, task: str, provider: str, model: str) -> str:
    """Pin ``auxiliary.<task>.provider`` and ``.model`` to the given values.

    - Replaces existing 4-space ``provider:`` and ``model:`` lines if present.
    - Inserts them right after the task header if not.
    - Leaves every other field (timeout, base_url, api_key, extra_body, …) alone.

    Returns the text unchanged when the auxiliary or task block isn't present
    (so we don't fabricate sections that the user removed on purpose).
    """
    span = _auxiliary_task_span(text, task)
    if span is None:
        return text
    lines = text.splitlines(keepends=True)
    start, end = span
    block = lines[start:end]
    new_block: list[str] = [block[0]]  # keep "  <task>:" header
    seen_provider = False
    seen_model = False
    for ln in block[1:]:
        stripped = ln.lstrip(" ")
        indent = len(ln) - len(stripped)
        if indent == 4 and stripped.startswith("provider:"):
            new_block.append(f"    provider: {provider}\n")
            seen_provider = True
            continue
        if indent == 4 and stripped.startswith("model:"):
            new_block.append(f"    model: {model}\n")
            seen_model = True
            continue
        new_block.append(ln)
    if not seen_provider:
        new_block.insert(1, f"    provider: {provider}\n")
    if not seen_model:
        # insert right after provider (or right after header if no provider)
        insert_at = 2 if seen_provider or not seen_provider else 1
        new_block.insert(insert_at, f"    model: {model}\n")
    return "".join(lines[:start]) + "".join(new_block) + "".join(lines[end:])


def rewrite_provider_block(text: str, slug: str, models: list[str]) -> str:
    """Surgically rewrite the volcengine provider block.

    - Ensure `discover_models: false` is present (insert after base_url).
    - Replace any existing `models:` block (string list or dict-style) with a
      fresh string-only list using `models`.

    Preserves all other lines (api_key, base_url, model, name, comments).
    """
    span = _provider_block_span(text, slug)
    if span is None:
        raise RuntimeError(f"could not locate provider block for slug={slug!r}")
    lines = text.splitlines(keepends=True)
    start, end = span
    block = lines[start:end]

    # Phase 1: drop the existing models: block (if any).  models: is a 4-space
    # indented key inside the provider block (which itself is at 2-space).
    new_block: list[str] = []
    skipping_models = False
    for ln in block:
        stripped = ln.lstrip(" ")
        indent = len(ln) - len(stripped)
        if skipping_models:
            # we keep skipping while indent > 4 (children of models:)
            if not ln.strip():
                continue  # drop blank lines inside models: block
            if indent > 4 or (indent == 4 and stripped.startswith("- ")):
                continue
            # back to provider-level — stop skipping, fall through to keep this line
            skipping_models = False
        if indent == 4 and stripped.rstrip().rstrip(":") == "models" and stripped.rstrip().endswith(":"):
            skipping_models = True
            continue
        new_block.append(ln)

    # Phase 2: ensure discover_models: false present.  Place it right after
    # base_url / api line if not already there.
    has_discover = any(
        l.lstrip(" ").startswith("discover_models:") and (len(l) - len(l.lstrip(" "))) == 4
        for l in new_block
    )
    if not has_discover:
        for i, ln in enumerate(new_block):
            stripped = ln.lstrip(" ")
            indent = len(ln) - len(stripped)
            if indent == 4 and (
                stripped.startswith("base_url:")
                or stripped.startswith("api:")
                or stripped.startswith("url:")
            ):
                new_block.insert(i + 1, "    discover_models: false\n")
                break
        else:
            # no base_url found — append at end of block
            new_block.append("    discover_models: false\n")
    else:
        # already there: normalize value to false
        for i, ln in enumerate(new_block):
            if ln.lstrip(" ").startswith("discover_models:"):
                new_block[i] = "    discover_models: false\n"
                break

    # Phase 3: append a fresh models: block at the end of the provider block.
    # Strip trailing blank lines from new_block, then add models: + entries +
    # a trailing newline (so the next provider stays separated).
    while new_block and not new_block[-1].strip():
        new_block.pop()
    new_block.append("    models:\n")
    for m in models:
        new_block.append(f"    - {m}\n")

    # Re-stitch
    return "".join(lines[:start]) + "".join(new_block) + "".join(lines[end:])


# Curated default model list for火山 ark coding/v3.  Includes both:
#  - Aliases the endpoint accepts but doesn't list in /v1/models (glm-5.x,
#    kimi-k2.x, minimax-m2.7, ark-code-latest, etc.).
#  - All currently-live text LLMs from /v1/models (status != Shutdown/Retiring,
#    domain != Embedding/Video/Image/3D) as of 2026-06-18.
# Override with --models "a,b,c" to use a custom subset.
DEFAULT_MODELS = [
    # ── Aliases (not in /v1/models but endpoint accepts) ──
    "glm-5.2",
    "glm-5.1",
    "glm-4.7",
    "kimi-k2.6",
    "kimi-k2.5",
    "minimax-m2.7",
    "ark-code-latest",
    # ── GLM 系列 ──
    "glm-4-5-air-20250728",
    "glm-4-7-251222",
    # ── Qwen 系列 ──
    "qwen2-5-72b-20240919",
    "qwen3-0-6b-20250429",
    "qwen3-8b-20250429",
    "qwen3-14b-20250429",
    "qwen3-32b-20250429",
    # ── Doubao 1.5 ──
    "doubao-1-5-lite-32k-250115",
    "doubao-1-5-pro-32k-250115",
    "doubao-1-5-pro-32k-character-250715",
    "doubao-1-5-vision-pro-32k-250115",
    # ── Doubao seed 1.6 ──
    "doubao-seed-1-6-250615",
    "doubao-seed-1-6-251015",
    "doubao-seed-1-6-flash-250615",
    "doubao-seed-1-6-flash-250828",
    "doubao-seed-1-6-vision-250815",
    "doubao-seed-code-preview-251028",
    # ── Doubao seed 1.8 / 2.0 ──
    "doubao-seed-1-8-251228",
    "doubao-seed-2-0-lite-260215",
    "doubao-seed-2-0-lite-260428",
    "doubao-seed-2-0-mini-260215",
    "doubao-seed-2-0-mini-260428",
    "doubao-seed-2-0-pro-260215",
    "doubao-seed-2-0-code-preview-260215",
    # ── DeepSeek ──
    "deepseek-v3-2-251201",
    "deepseek-v4-pro-260425",
    "deepseek-v4-flash-260425",
    # ── 其它 ──
    "doubao-seed-translation-250915",
    "doubao-seed-character-251128",
    "doubao-smart-router-250928",
]


def make_backup(path: Path) -> Path:
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = path.with_name(path.name + f".bak-{ts}")
    shutil.copy2(path, bak)
    return bak


def newest_backup(path: Path) -> Path | None:
    cands = sorted(path.parent.glob(path.name + ".bak-*"), reverse=True)
    return cands[0] if cands else None


def cmd_rollback(cfg_path: Path) -> int:
    if not cfg_path.exists():
        err(f"{cfg_path} 不存在")
        return 1
    bak = newest_backup(cfg_path)
    if not bak:
        err("没找到 .bak-* 备份文件")
        return 1
    info(f"将从 {bak.name} 还原 → {cfg_path}")
    shutil.copy2(bak, cfg_path)
    ok("已还原")
    return 0


def reload_desktop_if_running() -> None:
    """If Hermes Desktop is running, ask it to reload by killing & relaunching.
    macOS only.  Best-effort, never fails the script.
    """
    if sys.platform != "darwin":
        return
    try:
        result = subprocess.run(
            ["pgrep", "-x", "Hermes"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return
    if not result.stdout.strip():
        return
    info("Hermes Desktop 在运行 — 重启它以加载新配置")
    try:
        subprocess.run(["pkill", "-x", "Hermes"], timeout=5)
        # give it a moment to die
        import time
        time.sleep(2)
        subprocess.run(["open", "-a", "Hermes"], timeout=5)
        ok("Hermes Desktop 已重启")
    except Exception as e:
        warn(f"重启 Desktop 失败：{e}（请手动重启）")


def confirm(prompt: str, default_yes: bool = True) -> bool:
    suffix = "[Y/n]" if default_yes else "[y/N]"
    try:
        ans = input(f"{prompt} {suffix} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not ans:
        return default_yes
    return ans in ("y", "yes")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="fix-volcengine-glm",
        description="一键修复 Hermes Desktop 看不到火山引擎 coding plan 模型（glm-5.2 等）的问题。",
    )
    parser.add_argument("--yes", "-y", action="store_true", help="自动确认所有修改")
    parser.add_argument("--dry-run", action="store_true", help="只显示会做什么，不写文件")
    parser.add_argument("--rollback", action="store_true", help="从最近的 .bak-* 还原 config")
    parser.add_argument(
        "--models",
        type=str,
        default="",
        help="自定义模型列表，逗号分隔（默认包含 glm-5.2 等 12 个常用别名）",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=str(CONFIG),
        help=f"config.yaml 路径（默认 {CONFIG}）",
    )
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="不要自动重启 Hermes Desktop（默认会重启正在运行的 Desktop）",
    )
    args = parser.parse_args()

    cfg_path = Path(args.config).expanduser()

    if args.rollback:
        return cmd_rollback(cfg_path)

    if not cfg_path.exists():
        err(f"找不到 Hermes 配置文件：{cfg_path}")
        err("提示：确认 Hermes Agent 已安装。CLI 一般会创建 ~/.hermes/config.yaml")
        return 1

    if not have_yaml():
        err("需要 PyYAML：pip install pyyaml")
        return 1

    print(c(f"━━━ Hermes Volcengine 模型可见性修复工具 ━━━", BOLD))
    info(f"读取 {cfg_path}")
    cfg = load_yaml(cfg_path) or {}

    diag = diagnose(cfg)
    if diag["state"] == "no_volcengine":
        warn("未找到火山引擎（ark.volces）provider — 没什么要修的。")
        info("如果你刚配置 Hermes、还没加 volcengine-coding-plan，请先：")
        info("  hermes setup   # 或者手动在 ~/.hermes/config.yaml 里加 providers")
        return 0

    slug = diag["slug"]
    print()
    info(f"找到 provider：{c(slug, BOLD)}")
    info(f"  当前默认模型：{diag['default_model'] or c('(未设置)', DIM)}")
    info(f"  discover_models：{diag['discover_models']}  "
         f"{c('(会被 live API 覆盖)', YELLOW) if diag['discover_models'] is not False else c('(已正确)', GREEN)}")
    info(f"  models 字段：{len(diag['models_list'])} 个 "
         f"{c('(dict 格式 — 会让 inventory 崩)', RED) if diag['dict_style_models'] else ''}")

    # decide model list
    if args.models:
        models = [m.strip() for m in args.models.split(",") if m.strip()]
    else:
        # merge: keep user's existing string entries, then add defaults that aren't present
        existing = list(diag["models_list"])
        models = list(existing)
        for m in DEFAULT_MODELS:
            if m not in models:
                models.append(m)
        # ensure default model is at the top
        dm = diag["default_model"]
        if dm and dm in models:
            models.remove(dm)
            models.insert(0, dm)
        elif dm:
            models.insert(0, dm)

    # already fully fixed?
    aux_tasks_auto = diag.get("aux_tasks_auto") or []
    already_ok = (
        diag["discover_models"] is False
        and not diag["dict_style_models"]
        and set(diag["models_list"]) >= set(DEFAULT_MODELS)
        and not aux_tasks_auto
    )
    if already_ok:
        ok("配置看起来已经修过了 — 无需更改。")
        if not args.no_restart:
            reload_desktop_if_running()
        return 0

    print()
    info("将进行以下修改：")
    if diag["discover_models"] is not False:
        print(f"  • 添加 {c('discover_models: false', BOLD)}（关闭 live API 覆盖）")
    if diag["dict_style_models"]:
        print(f"  • 把 dict 格式的 models 转成纯字符串列表（避免 inventory 崩）")
    if set(diag["models_list"]) < set(DEFAULT_MODELS):
        print(f"  • 重写 models 列表，含 {len(models)} 项：")
        preview = models[:6]
        print(f"    {', '.join(preview)}{', …' if len(models) > 6 else ''}")
    if aux_tasks_auto:
        pin_model = diag["default_model"] or (models[0] if models else "glm-5.2")
        print(f"  • 把 {len(aux_tasks_auto)} 个 auxiliary 任务从 {c('auto', YELLOW)} 改为 "
              f"{c(slug, BOLD)} + {c(pin_model, BOLD)}")
        print(f"    （修复 \"No LLM provider configured for task=vision\" 的报错）")
        print(f"    任务：{', '.join(aux_tasks_auto)}")
    print()

    if args.dry_run:
        info("--dry-run 模式 — 不写文件。")
        if set(diag["models_list"]) < set(DEFAULT_MODELS):
            info("完整新 models 列表：")
            for m in models:
                print(f"    - {m}")
        return 0

    if not args.yes:
        if not confirm("继续？", default_yes=True):
            warn("已取消")
            return 1

    text = cfg_path.read_text(encoding="utf-8")
    new_text = rewrite_provider_block(text, slug, models)
    # Pin auxiliary tasks to the main provider/model so vision/web_extract/
    # compression/etc don't fall through the broken auto chain when the main
    # provider is a named-custom one.
    if aux_tasks_auto:
        pin_provider = slug
        pin_model = diag["default_model"] or (models[0] if models else "glm-5.2")
        for task in aux_tasks_auto:
            new_text = rewrite_auxiliary_task(new_text, task, pin_provider, pin_model)

    if new_text == text:
        warn("文本无变化（可能是匹配脚本错过 — 请检查 provider 缩进是否为 2 空格）")
        return 1

    bak = make_backup(cfg_path)
    ok(f"备份：{bak.name}")
    cfg_path.write_text(new_text, encoding="utf-8")
    ok("config.yaml 已更新")

    # validate by re-parsing
    try:
        new_cfg = load_yaml(cfg_path) or {}
        new_diag = diagnose(new_cfg)
        assert new_diag["state"] == "found"
        assert new_diag["discover_models"] is False
        assert not new_diag["dict_style_models"]
        assert not new_diag.get("aux_tasks_auto")
        assert "glm-5.2" in new_diag["models_list"] or any(
            m in new_diag["models_list"] for m in models
        )
        ok("YAML 解析通过，关键字段已生效")
    except Exception as e:
        err(f"自检失败：{e}")
        err(f"已自动回滚 → {bak}")
        shutil.copy2(bak, cfg_path)
        return 1

    if not args.no_restart:
        print()
        reload_desktop_if_running()

    print()
    ok(c("修复完成！", GREEN))
    info("现在打开 Hermes Desktop 的模型选择器，应该能看到 glm-5.2 等模型了。")
    info(f"如需还原：{c(f'python3 {Path(__file__).name} --rollback', BOLD)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
