# Telegram Summary Tool

从 Telegram 聊天记录生成结构化 Markdown 摘要的命令行工具。

支持三种数据源：
- `tgmix`（默认）：解析 Telegram Desktop 导出的 `result.json`，调用开源 [tgmix](https://pypi.org/project/tgmix/) 预处理
- `raw-export`：直接解析 `result.json`，无需 tgmix
- `local`（仅 macOS）：直接读取本地 Telegram 加密数据库

## 依赖要求

| 依赖 | 必需？ | 用途 |
|------|--------|------|
| Python 3.9+ | 必需 | 核心运行时 |
| [uv](https://docs.astral.sh/uv/) (`uvx`) | tgmix 模式需要 | 运行 tgmix |
| Node.js 18+ | GramJS 连接器需要 | 直接从 Telegram 拉消息 |
| [sqlcipher](https://www.zetetic.net/sqlcipher/) | local 模式需要 | 解密本地 Telegram 数据库 |
| [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) 或 [Codex CLI](https://github.com/openai/codex) | AI 分析需要 | 本地生成智能摘要 |

## 3 分钟开箱

```bash
# 1) 克隆
# 替换为你的仓库地址
git clone https://github.com/<your-username>/telegram-summary-tool.git
cd telegram-summary-tool

# 2) 安装
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# 3) 环境体检（新增）
tg-doctor

# 4) 用内置 demo 数据跑通（无需 Telegram 账号）
tg-summary examples/sample-result.json --source raw-export --days 7 --output summary.md
```

如果第 4 步成功，会在当前目录生成 `summary.md`。

## 常用用法

### A) Telegram Desktop 导出 + tgmix（默认）

1. Telegram Desktop 导出聊天，格式选择 `Machine-readable JSON`
2. 导出目录中应包含 `result.json`

```bash
# 安装 uv（如果还没装）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 生成摘要
tg-summary /path/to/telegram_export --days 1 --output summary.md
```

更多选项：

```bash
tg-summary /path/to/telegram_export \
  --start 2026-02-20 \
  --end 2026-02-25 \
  --anonymize \
  --reuse-preprocessed \
  --output weekly-summary.md
```

### B) Raw export（无需 tgmix）

```bash
tg-summary /path/to/result.json --source raw-export --days 1 --output summary.md
```

### C) 本地数据库模式（仅 macOS）

> 需要 App Store 版 Telegram + `sqlcipher`。

```bash
brew install sqlcipher

# 列出可用群组
tg-summary --source local --list-chats

# 生成指定群组摘要
tg-summary --source local --chat-id <PEER_ID> --days 7 --output summary.md
```

### Web 界面（仅 macOS local 模式）

```bash
source .venv/bin/activate
tg-web
# 浏览器打开 http://127.0.0.1:8877
```

## AI 功能前置条件

AI 分析通过本地 CLI 调用，不走云端 API Key。需要满足：

1. 已安装 `claude` 或 `codex` 命令
2. 已在本机完成 CLI 登录（例如 `claude login` 或 `codex login`）
3. `tg-doctor` 输出中 `AI summary` 为 `OK`

如果 AI 分析超时，可以尝试：
- 切到更快模型（例如 Haiku）
- 降低采样上限
- 提高超时时间

## GramJS 连接器（可选）

直接从 Telegram 账号拉消息，无需手动导出。

```bash
# 1) 安装依赖
cd connectors/gramjs
npm install

# 2) 凭据从 https://my.telegram.org 获取
TG_API_ID=123456 TG_API_HASH=your_hash node fetch-chat.mjs \
  --chat your_group_username --limit 800 --out ./result.json

# 3) 生成摘要
cd ../..
source .venv/bin/activate
tg-summary connectors/gramjs/result.json --source raw-export --days 1 --output summary.md
```

## CLI 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `input` | - | Telegram 导出目录或 result.json 路径 |
| `--source` | `tgmix` | `tgmix` / `raw-export` / `local` |
| `--days` | `1` | 回溯天数 |
| `--start` | - | 开始日期 (YYYY-MM-DD) |
| `--end` | - | 结束日期 (YYYY-MM-DD，含当天) |
| `--top-users` | `10` | 活跃用户排行数 |
| `--top-keywords` | `20` | 热门关键词数 |
| `--max-actions` | `12` | 最大待办事项数 |
| `--anonymize` | `false` | tgmix 匿名化 |
| `--reuse-preprocessed` | `false` | 复用 `tgmix_output.toon.txt` |
| `--output` | stdout | 输出文件路径 |
| `--list-chats` | - | 列出本地数据库中的群组 |
| `--chat-id` | - | 指定群组 ID（local 模式） |

## 平台限制

- `local` 和 Web 界面仅支持 macOS（App Store 版 Telegram）
- `tgmix` 和 `raw-export` 支持跨平台
- AI 功能依赖本地 CLI（`claude` 或 `codex`）

## 发布前建议

准备公开到 GitHub 前，建议按清单检查：
- [PUBLISH_CHECKLIST.md](PUBLISH_CHECKLIST.md)

## License

MIT
