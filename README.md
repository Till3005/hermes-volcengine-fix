# fix-volcengine-models

[![tests](https://github.com/Till3005/hermes-volcengine-fix/actions/workflows/tests.yml/badge.svg)](https://github.com/Till3005/hermes-volcengine-fix/actions/workflows/tests.yml)

一键修复 **Hermes Desktop 看不到火山引擎 coding plan 模型**（glm-5.2、kimi-k2.6 等）的问题。

## 说的就是你

你在用 [Hermes Agent](https://hermes-agent.nousresearch.com) + 火山引擎 coding plan
（base_url 是 `https://ark.cn-beijing.volces.com/api/coding/v3`），CLI 跑得好好的、
但 **Hermes Desktop 的模型下拉里看不到 glm-5.2** —— 那就是这个工具要解决的问题。

## 一行命令修复

```bash
curl -fsSL https://raw.githubusercontent.com/Till3005/hermes-volcengine-fix/main/fix.py \
  | python3 - --yes
```

或者下载脚本本地跑：

```bash
curl -fsSLO https://raw.githubusercontent.com/Till3005/hermes-volcengine-fix/main/fix.py
python3 fix.py            # 交互式确认
python3 fix.py --yes      # 不询问直接修
python3 fix.py --dry-run  # 只看会改什么
python3 fix.py --rollback # 从最近备份还原
```

依赖：Python 3.8+ 和 `pyyaml`（Hermes 自己的 venv 已经带了，直接用它即可）：

```bash
~/.hermes/hermes-agent/venv/bin/python fix.py --yes
```

## 它做了什么

工具会自动：

1. **检测** `~/.hermes/config.yaml` 里指向 ark.volces 的 provider
2. **备份** 当前配置到 `config.yaml.bak-YYYYMMDD-HHMMSS`
3. **修复** provider 配置：
   - 添加 `discover_models: false` —— 阻止 Hermes 调用 live `/v1/models` 覆盖你写的列表
   - 把 dict 格式的 `models:` 转成纯字符串列表（dict 格式会让 Hermes inventory 崩）
   - 写入一份包含 glm-5.2、kimi-k2.6、deepseek-v3.2 等 12 个常用别名的列表
4. **验证** 改完之后能正常解析、关键字段已生效；失败自动回滚
5. **重启** 正在运行的 Hermes Desktop（macOS）

其他 provider、注释、缩进、以及 config 里所有别的字段全部保持原样。

## 为什么会有这个问题

短版本：Hermes Desktop 的模型下拉来自后端 `/api/model/options`，对每个有 api_key 的
custom provider 默认会调一次 live `/v1/models`，**用返回结果覆盖**你在 config.yaml
里手写的 `models:`。

火山 ark coding/v3 的 `/v1/models` 返回了几百个 doubao/qwen/deepseek 模型，但
**没有 glm-5.2、kimi-k2.6 这些常用别名** —— 实际上这些模型 endpoint 是接受的，列表里只是没列。
所以 CLI 直接用 `model: glm-5.2` 能跑通，但 Desktop 下拉拿不到。

修复方法是给 provider 加上 `discover_models: false`，告诉 Hermes 别去 probe，
直接用 config 里手写的列表。

## 自定义模型列表

默认包含：`glm-5.2, glm-5.1, glm-4.7, kimi-k2.6, kimi-k2.5, minimax-m2.7, deepseek-v3.2,
doubao-seed-2.0-pro, doubao-seed-2.0-code, doubao-seed-2.0-lite, doubao-seed-code,
ark-code-latest`。

要加别的：

```bash
python3 fix.py --models "glm-5.2,kimi-k2.6,my-custom-alias" --yes
```

## 还原

```bash
python3 fix.py --rollback
```

会从最近的 `config.yaml.bak-*` 备份还原。

## 兼容性

- macOS / Linux （Windows 没测；逻辑应该都通，只是不会自动重启 Desktop）
- Python 3.8+
- Hermes Agent 任意版本（脚本不依赖 Hermes 的 Python 包，只读写 config.yaml）

## 许可

MIT
