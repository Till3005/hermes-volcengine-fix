#!/usr/bin/env python3
"""Self-test for fix.py — exercises the major scenarios end-to-end.

Run from the repo root:

    python3 tests/test_fix.py

Returns non-zero on any failure. Used by CI.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "fix.py"
PY = sys.executable


# ─── Fixtures ────────────────────────────────────────────────────────────

DICT_STYLE = """\
model:
  provider: volcengine-coding-plan
  default: glm-5.2
providers:
  volcengine-coding-plan:
    name: Volcengine Coding Plan
    base_url: https://ark.cn-beijing.volces.com/api/coding/v3
    api_key: ark-FAKE
    model: glm-5.2
    models:
    - id: ark-code-latest
      context_length: 256000
    - id: doubao-seed-2.0-pro
      context_length: 256000
  # comment between providers — must survive
  openai:
    base_url: https://api.openai.com/v1
    api_key: sk-fake
agent:
  max_turns: 90
"""

NO_DISCOVER_STRING_LIST = """\
model:
  provider: volcengine-coding-plan
  default: glm-5.2
providers:
  volcengine-coding-plan:
    base_url: https://ark.cn-beijing.volces.com/api/coding/v3
    api_key: ark-FAKE
    model: glm-5.2
    models:
    - glm-5.2
    - ark-code-latest
"""

ALREADY_FIXED = """\
model:
  provider: volcengine-coding-plan
  default: glm-5.2
providers:
  volcengine-coding-plan:
    base_url: https://ark.cn-beijing.volces.com/api/coding/v3
    discover_models: false
    api_key: ark-FAKE
    model: glm-5.2
    models:
    - glm-5.2
    - glm-5.1
    - glm-4.7
    - kimi-k2.6
    - kimi-k2.5
    - minimax-m2.7
    - deepseek-v3.2
    - doubao-seed-2.0-pro
    - doubao-seed-2.0-code
    - doubao-seed-2.0-lite
    - doubao-seed-code
    - ark-code-latest
"""

AUTO_AUXILIARY = """\
model:
  provider: volcengine-coding-plan
  default: glm-5.2
providers:
  volcengine-coding-plan:
    base_url: https://ark.cn-beijing.volces.com/api/coding/v3
    discover_models: false
    api_key: ark-FAKE
    model: glm-5.2
    models:
    - glm-5.2
    - glm-5.1
    - glm-4.7
    - kimi-k2.6
    - kimi-k2.5
    - minimax-m2.7
    - deepseek-v3.2
    - doubao-seed-2.0-pro
    - doubao-seed-2.0-code
    - doubao-seed-2.0-lite
    - doubao-seed-code
    - ark-code-latest
auxiliary:
  vision:
    provider: auto
    model: ''
    base_url: ''
    api_key: ''
    timeout: 120
  web_extract:
    provider: ''
    model: ''
    timeout: 60
  triage_specifier:
    provider: auto
    model: ''
    timeout: 30
"""

MIXED_AUXILIARY = """\
model:
  provider: volcengine-coding-plan
  default: glm-5.2
providers:
  volcengine-coding-plan:
    base_url: https://ark.cn-beijing.volces.com/api/coding/v3
    discover_models: false
    api_key: ark-FAKE
    model: glm-5.2
    models:
    - glm-5.2
    - glm-5.1
    - glm-4.7
    - kimi-k2.6
    - kimi-k2.5
    - minimax-m2.7
    - deepseek-v3.2
    - doubao-seed-2.0-pro
    - doubao-seed-2.0-code
    - doubao-seed-2.0-lite
    - doubao-seed-code
    - ark-code-latest
auxiliary:
  vision:
    provider: volcengine-coding-plan
    model: glm-5.2
    timeout: 120
  web_extract:
    provider: openai
    model: gpt-4o-mini
    timeout: 60
"""

NO_VOLCENGINE = """\
model:
  provider: openai
  default: gpt-4o
providers:
  openai:
    api_key: sk-fake
"""


# ─── Test helpers ────────────────────────────────────────────────────────


def run_fix(*args: str) -> subprocess.CompletedProcess:
    """Invoke fix.py with the given args and return CompletedProcess."""
    return subprocess.run(
        [PY, str(FIX), *args],
        capture_output=True,
        text=True,
        env={**os.environ, "NO_COLOR": "1"},
    )


def write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def load_yaml(path: Path):
    import yaml
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ─── Tests ───────────────────────────────────────────────────────────────


def test_dict_style_fixed():
    """Dict-style models gets converted to string list + discover_models added."""
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "config.yaml"
        write(cfg, DICT_STYLE)
        r = run_fix("--config", str(cfg), "--no-restart", "--yes")
        assert r.returncode == 0, f"non-zero exit: {r.stderr}"
        data = load_yaml(cfg)
        prov = data["providers"]["volcengine-coding-plan"]
        assert prov["discover_models"] is False, "discover_models not set"
        models = prov["models"]
        assert isinstance(models, list)
        assert all(isinstance(m, str) for m in models), f"got non-string: {models}"
        assert "glm-5.2" in models
        assert models[0] == "glm-5.2", f"default model not first: {models[0]}"
        # other providers preserved
        assert "openai" in data["providers"]
        # comment preserved
        assert "comment between providers" in cfg.read_text()
        # backup created
        baks = list(cfg.parent.glob("config.yaml.bak-*"))
        assert len(baks) == 1


def test_idempotent():
    """Running fix on already-fixed config should be a no-op (no backup)."""
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "config.yaml"
        write(cfg, ALREADY_FIXED)
        before = cfg.read_text()
        r = run_fix("--config", str(cfg), "--no-restart", "--yes")
        assert r.returncode == 0
        assert "已经修过" in r.stdout or "已修过" in r.stdout, r.stdout
        assert cfg.read_text() == before, "file changed despite already-fixed"
        assert not list(cfg.parent.glob("config.yaml.bak-*")), "spurious backup"


def test_auto_auxiliary_tasks_are_pinned_to_volcengine():
    """Known auto auxiliary tasks are pinned to the volcengine provider/model."""
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "config.yaml"
        write(cfg, AUTO_AUXILIARY)
        r = run_fix("--config", str(cfg), "--no-restart", "--yes")
        assert r.returncode == 0, r.stderr
        data = load_yaml(cfg)
        aux = data["auxiliary"]
        assert aux["vision"]["provider"] == "volcengine-coding-plan"
        assert aux["vision"]["model"] == "glm-5.2"
        assert aux["vision"]["timeout"] == 120
        assert aux["web_extract"]["provider"] == "volcengine-coding-plan"
        assert aux["web_extract"]["model"] == "glm-5.2"
        # Other auto tasks are outside the workaround's allowlist and remain unchanged.
        assert aux["triage_specifier"]["provider"] == "auto"


def test_auxiliary_only_fix_preserves_provider_text():
    """When only auxiliary tasks need fixes, the provider block is left byte-for-byte intact."""
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "config.yaml"
        before = AUTO_AUXILIARY
        write(cfg, before)
        r = run_fix("--config", str(cfg), "--no-restart", "--yes")
        assert r.returncode == 0, r.stderr
        after = cfg.read_text()
        before_provider = before.split("auxiliary:\n", 1)[0]
        after_provider = after.split("auxiliary:\n", 1)[0]
        assert after_provider == before_provider


def test_pinned_auxiliary_tasks_are_idempotent():
    """Explicit auxiliary provider/model choices should not be rewritten."""
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "config.yaml"
        write(cfg, MIXED_AUXILIARY)
        before = cfg.read_text()
        r = run_fix("--config", str(cfg), "--no-restart", "--yes")
        assert r.returncode == 0
        assert cfg.read_text() == before, "explicit auxiliary config was rewritten"
        assert not list(cfg.parent.glob("config.yaml.bak-*")), "spurious backup"


def test_string_list_needs_discover():
    """String-list models but missing discover_models still gets fixed."""
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "config.yaml"
        write(cfg, NO_DISCOVER_STRING_LIST)
        r = run_fix("--config", str(cfg), "--no-restart", "--yes")
        assert r.returncode == 0, r.stderr
        data = load_yaml(cfg)
        prov = data["providers"]["volcengine-coding-plan"]
        assert prov["discover_models"] is False


def test_dry_run_no_write():
    """--dry-run must not touch the file or create a backup."""
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "config.yaml"
        write(cfg, DICT_STYLE)
        before = cfg.read_text()
        r = run_fix("--config", str(cfg), "--no-restart", "--dry-run", "--yes")
        assert r.returncode == 0
        assert cfg.read_text() == before, "dry-run modified file"
        assert not list(cfg.parent.glob("config.yaml.bak-*")), "dry-run made backup"


def test_rollback():
    """Fix then rollback restores original content."""
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "config.yaml"
        write(cfg, DICT_STYLE)
        original = cfg.read_text()
        r = run_fix("--config", str(cfg), "--no-restart", "--yes")
        assert r.returncode == 0
        assert cfg.read_text() != original, "fix didn't change file"
        r = run_fix("--config", str(cfg), "--rollback")
        assert r.returncode == 0
        assert cfg.read_text() == original, "rollback didn't restore"


def test_no_volcengine():
    """Config without a volcengine provider exits cleanly."""
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "config.yaml"
        write(cfg, NO_VOLCENGINE)
        before = cfg.read_text()
        r = run_fix("--config", str(cfg), "--no-restart", "--yes")
        assert r.returncode == 0
        assert cfg.read_text() == before, "file changed despite no volcengine"


def test_missing_config():
    """Nonexistent config file returns non-zero."""
    r = run_fix("--config", "/tmp/does-not-exist-12345.yaml", "--no-restart", "--yes")
    assert r.returncode != 0


def test_custom_models():
    """--models flag overrides the default list."""
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "config.yaml"
        write(cfg, DICT_STYLE)
        r = run_fix(
            "--config", str(cfg),
            "--no-restart", "--yes",
            "--models", "glm-5.2,my-custom-alias,kimi-k2.6",
        )
        assert r.returncode == 0
        data = load_yaml(cfg)
        models = data["providers"]["volcengine-coding-plan"]["models"]
        assert models == ["glm-5.2", "my-custom-alias", "kimi-k2.6"], models


# ─── Runner ──────────────────────────────────────────────────────────────


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        name = t.__name__
        try:
            t()
            print(f"  ✓ {name}")
        except AssertionError as e:
            print(f"  ✗ {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ {name}: unexpected {type(e).__name__}: {e}")
            failed += 1
    total = len(tests)
    print(f"\n{total - failed}/{total} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
