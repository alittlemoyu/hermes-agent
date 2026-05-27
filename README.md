# Moyu Hermes Agent Fork

这是 `alittlemoyu/hermes-agent` 的本地维护 fork，基于
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)，用于承载墨羽当前运行中的 Hermes 环境、本地记忆体系、OpenViking provider 集成和外置插件边界。

如果你想看官方 Hermes Agent 的产品介绍、安装文档和通用功能说明，请看：

- 官方仓库：<https://github.com/NousResearch/hermes-agent>
- 官方文档：<https://hermes-agent.nousresearch.com/docs/>

这个 README 只说明本 fork 的维护边界。

## 当前定位

本仓不是从零改写 Hermes，也不是通用发行版。它的目标是：

- 跟随官方 Hermes upstream，尽量保持可 rebase / 可 cherry-pick。
- 保留本地运行所需的 provider、gateway、OpenViking、Weixin 等补丁。
- 把个人记忆系统的执行规则固化到工具代码和 schema/result contract 中，而不是让 LLM 只靠 AGENTS.md 背规则。
- 将可独立维护的资料层插件拆到独立仓库，避免污染 Hermes 主仓。

## 仓库边界

| 仓库 | 角色 |
| --- | --- |
| `alittlemoyu/hermes-agent` | Hermes fork。保留核心 runtime、gateway、OpenViking memory provider、本地必要补丁。 |
| `alittlemoyu/OpenViking` | OpenViking fork。保留本地 Codex/Claude memory plugin 集成补丁。 |
| `alittlemoyu/hermes-factmemory-plugin` | factmemory 独立 Hermes 插件。结构化记忆事实源、workflow、lifecycle、report/project 视图。 |
| `alittlemoyu/hermes-document-memory` | document_memory 独立 Hermes 插件。文件/URL 注册、OpenViking 索引、RAG evidence packet。 |

本仓已经不再内置 `plugins/document_memory`。document memory 应安装在：

```bash
~/.hermes/plugins/document_memory
```

factmemory 应安装在：

```bash
~/.hermes/plugins/factmemory
```

## 本 fork 的关键改动

### OpenViking memory provider

OpenViking 是 Hermes 的 memory provider 和 recall/archive/mirror 层，不是 factmemory 的事实源。

本 fork 在 `plugins/memory/openviking` 中保留：

- OpenViking session capture / commit / archive lifecycle。
- `viking_*` 工具集。
- `tool_contract`、`write_policy`、`recovery_hint`、`dangerous_misroutes` 等 schema/result contract。
- 对 factmemory mirror 的只读边界说明：OpenViking 召回结果只能作为候选线索，不能反向覆盖 `.memory/entities`。

### 外置 factmemory 插件

factmemory 插件负责正式结构化记忆：

- daily log 是原始记录。
- `.memory/entities` 是 Project / Task / Fact / Problem 的事实源。
- 普通写入主路径是 `context/search -> log -> stage -> commit`。
- legacy `dry_run -> ingest` 只保留兼容和维护用途。
- lifecycle candidates 只是候选/经验层，只有显式人工确认后才成为可注入经验。

规则必须在插件实现、schema、handler 返回体和测试中同时成立。

### 外置 document_memory 插件

document_memory 是资料/RAG evidence 层：

- 注册本地文件或 URL。
- 通过 OpenViking 建索引。
- 支持 search/read/list/link。
- `document_promote_to_fact` 只产出 evidence packet，不直接写 factmemory。

资料内容不能自动变成 Project / Task / Fact / Problem。

## 开发与验证

Hermes 主仓常用验证：

```bash
/home/moyu/.hermes/hermes-agent/venv/bin/pytest \
  tests/plugins/memory/test_openviking_provider.py \
  tests/openviking_plugin/test_openviking.py \
  tests/hermes_cli/test_plugin_cli_registration.py -q
```

factmemory 插件验证：

```bash
cd /home/moyu/.hermes/plugins/factmemory
/home/moyu/.hermes/hermes-agent/venv/bin/pytest tests/test_factmemory_core.py -q
```

document_memory 插件验证：

```bash
cd /home/moyu/.hermes/plugins/document_memory
/home/moyu/.hermes/hermes-agent/venv/bin/pytest tests/test_document_memory.py -q
```

OpenViking 运行态验证：

```bash
openviking --version
openviking status
curl -s http://127.0.0.1:1933/api/v1/debug/vector/count
```

## 维护规则

- `origin` 保持指向官方 upstream：`NousResearch/hermes-agent`。
- `fork` 指向个人 fork：`alittlemoyu/hermes-agent`。
- 提交前先确认 `git diff --stat` 和 `git status --short --branch`。
- 不把 `__pycache__`、`.pytest_cache`、构建产物、local registry 数据提交进仓库。
- 外置插件改动应提交到各自插件仓，不放回 Hermes 主仓。
- 如果某条规则影响 LLM 如何调用工具，优先改 schema、handler input error、返回体 contract 和测试，而不是只改 AGENTS.md。

## 当前 GitHub 入口

- Hermes fork: <https://github.com/alittlemoyu/hermes-agent>
- OpenViking fork: <https://github.com/alittlemoyu/OpenViking>
- factmemory plugin: <https://github.com/alittlemoyu/hermes-factmemory-plugin>
- document memory plugin: <https://github.com/alittlemoyu/hermes-document-memory>

## License

Hermes Agent upstream is MIT licensed. See [LICENSE](LICENSE).

Original project by [Nous Research](https://nousresearch.com).
