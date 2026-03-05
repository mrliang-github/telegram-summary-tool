"""
AI 摘要模块：借用本地 Claude Code CLI 或 Codex CLI 对聊天消息进行智能分析。

优先级：
1. claude CLI（已登录即可用，无需 API Key）
2. codex CLI（已登录即可用，无需 API Key）

用法：
    from .ai_summarizer import generate_ai_summary
    result = await generate_ai_summary(messages, chat_name="群名")
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from typing import List, Optional

from .telegram_export import TelegramMessage

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 配置常量
# ──────────────────────────────────────────────

# 发送给 AI 的最大消息条数（避免超出上下文窗口）
MAX_SAMPLE_MESSAGES = 500

# CLI 超时时间（秒）— 大群消息多时 Claude 分析需要较长时间
CLI_TIMEOUT = 300

# 默认模型（None = 使用 CLI 默认模型，可配置为 "claude-haiku-4-5-20251001" 等加速）
CLI_MODEL = None

# ──────────────────────────────────────────────
# 分析模板系统：预设不同场景的专精 Prompt
# ──────────────────────────────────────────────

# 通用群聊摘要（默认）
PROMPT_GENERAL = """你是一个专业的群聊分析师。用户会提供一个 Telegram 群组的聊天记录，请你：

1. **主题总结**（3-5个）：提炼本周群内讨论的核心话题，每个话题用 2-3 句话概括关键观点
2. **重要信息**：提取群友分享的重要资讯、数据、链接（如股票代码、市场分析、新闻等）
3. **观点交锋**：如果有明显分歧的讨论，列出不同观点
4. **氛围感知**：简单描述群内整体氛围（看多/看空/谨慎/乐观等）
5. **值得关注**：列出 2-3 条你认为最有价值的消息或建议

要求：
- 使用中文回复
- 保持客观，不添加个人判断
- 直接输出分析内容，不要加"以下是分析"之类的前缀
- 用 Markdown 格式组织输出"""

# 美股行情追踪
PROMPT_US_STOCK = """你是一个专业的美股群聊分析师。请从以下 Telegram 群聊记录中，提取所有与美股投资相关的有价值信息。

## 分析任务

### 1. 资产提及识别
识别所有被讨论的美股相关资产，包括：
- 个股（公司名/股票代码/别称，如"苹果""AAPL""果子"、"特斯拉""TSLA""大腿"、"英伟达""NVDA"）
- ETF（如"QQQ""SPY""ARKK"）
- 指数（如"纳指""标普""道琼斯""纳斯达克"）

对每个资产输出：标准名称、代码、被提及次数、讨论者列表

### 2. 操作信号提取
识别包含明确投资操作意图的消息：
- 建仓/买入："我买了""入场了""上车""建仓""开多""做多""call"
- 加仓："加仓""补仓""再买点""加码"
- 减仓/卖出："减仓""跑了""止盈""卖了""落袋""平仓"
- 止损："止损""割了""认亏""割肉""止损出局"
- 观望："再看看""等回调""不急""观望""等财报"
- 做空："做空""put""看跌""空了"

对每个信号标注：发言人、资产、操作方向、提及价格（如有）

### 3. 投资观点提取
识别包含分析逻辑的高质量观点（非闲聊）：
- 技术面："突破""支撑位""阻力位""MACD""均线""放量""缩量""头肩顶""金叉""死叉"
- 基本面："财报""PE""营收""EPS""指引""毛利率""增长""回购"
- 消息面/事件驱动："降息""加息""CPI""非农""FOMC""财报季""并购""拆股"
- 板块/趋势："AI 板块""芯片股""中概股""科技股""周期股""轮动""资金流向"

### 4. 重要性评分
对每条提取的信息按重要性打分（1-5）：
- 5分：带详细论据的操作建议（"NVDA 突破 950 阻力位放量，我在 955 建仓，目标 1050，止损 920"）
- 4分：带简单理由的推荐（"可以关注 AAPL，下周财报大概率超预期"）
- 3分：明确的情绪表达（"TSLA 要起飞了"）
- 2分：简单提及资产名（"NVDA 今天又新高了"）
- 1分：无实质内容的闲聊

### 5. 输出格式（严格按此格式，禁止使用 Markdown 表格语法）

---

#### 📊 资产热度排行

仅列出提及 2 次以上的资产，按提及次数降序。每个资产格式：

**`$代码`** 资产名称
• 提及 N 次 | 情绪：偏多 / 偏空 / 中性
• 一句话概括群内最核心的讨论点

---

#### 🎯 重要操作信号

仅展示 4-5 分信号，按时间排列，最多 5 条：

**信号｜** `$代码` · 做多 / 做空 / 止损 / 减仓
• @发言人 · 价格 $xxx（如有）
• 论据：一句话
> "相关原文引用"

无高质量信号则写"本期无明确操作信号"。

---

#### 📈 板块与趋势共识

列出 2-4 个群内讨论形成共识的方向：

**板块名称**
• 共识：群内主流判断
• 标的：$XXX、$XXX

---

#### 💡 高质量观点精选

3-5 条最有价值的带论据分析，保留发言人：

**@发言人**
• 论点：核心主张
• 论据：数据 / 逻辑 / 事件

---

#### ⚠️ 风险提示

按类别列出，无内容的类别省略：

**宏观风险：** 描述
**个股风险：** `$代码` - 风险点
**止损信号：** `$代码` $xxx 位置

### 注意事项
- 区分"认真分析"和"随口闲聊"，重点提取前者
- 4 分以下的信号不要出现在输出中
- 保留发言人名称（方便用户知道是谁说的）
- 如果有人互相辩论（看多 vs 看空），两方观点都要呈现
- 价格、数字要精确提取，不要模糊化
- 使用中文回复，用 Markdown 格式，直接输出分析内容"""

# 加密货币行情追踪
PROMPT_CRYPTO = """你是一个专业的加密货币群聊分析师。请从以下 Telegram 群聊记录中，提取所有与加密货币投资相关的有价值信息。

## 分析任务

### 1. 资产提及识别
识别所有被讨论的加密货币资产，包括：
- 主流币（BTC/比特币/大饼、ETH/以太坊/姨太/二饼、BNB/币安币、SOL/索拉纳）
- 山寨币/Altcoins（DOGE/狗狗/狗子、PEPE、ARB、OP、AVAX 等）
- Meme 币（及各种社区代称和缩写）
- DeFi/NFT 相关代币
- 交易对和合约（如"BTC/USDT 永续""ETH 合约"）

对每个资产输出：标准名称、代码、被提及次数、讨论者列表

### 2. 操作信号提取
识别包含明确操作意图的消息：
- 开多/做多："做多""开多""多了""上车""建仓""梭哈""冲了""all in"
- 开空/做空："做空""开空""空了""空单"
- 止盈/平仓："止盈""平仓""跑了""落袋""出了"
- 止损："止损""爆仓""割了""认亏""清算"
- 观望："等回调""不追高""观望""等插针"
- 现货买入："囤币""定投""现货买了""屯着"

标注：发言人、币种、方向、杠杆倍数（如有）、价格（如有）

### 3. 投资观点提取
识别包含分析逻辑的高质量观点：
- 链上数据："巨鲸""大户""链上转账""交易所流出/流入""持仓变化"
- 技术面："支撑""阻力""突破""回踩""插针""双底""头肩""趋势线"
- 基本面/生态："升级""TVL""Gas""质押""销毁""减半""解锁""空投"
- 宏观/资金面："美联储""降息""ETF 流入""合规""监管""灰度""机构"
- 情绪面："恐惧贪婪指数""爆仓数据""资金费率""多空比"

### 4. 重要性评分（1-5）
- 5分：带详细论据的操作建议（含价位、止损位、论据链）
- 4分：带简单理由的推荐（"SOL 生态最近很活跃，可以关注"）
- 3分：明确的情绪表达（"大饼要拉了"）
- 2分：简单提及（"ETH 跌了"）
- 1分：无实质内容

### 5. 输出格式（严格按此格式，禁止使用 Markdown 表格语法）

---

#### 📊 币种热度排行

仅列出提及 2 次以上的币种，按提及次数降序。每个币种格式：

**`$代码`** 币种名称
• 提及 N 次 | 情绪：偏多 / 偏空 / 中性
• 一句话概括群内最核心的讨论点

---

#### 🎯 重要操作信号

仅展示 4-5 分信号，按时间排列，最多 5 条：

**信号｜** `$代码` · 做多 / 做空 / 止损 / 减仓
• @发言人 · 杠杆 Nx（如有）· 价格 $xxx（如有）
• 论据：一句话
> "相关原文引用"

无高质量信号则写"本期无明确操作信号"。

---

#### 📈 叙事主线与板块趋势

列出 2-4 条当前群内关注的叙事主线：

**叙事主题**（如 AI+Crypto / L2 / Meme 季 / RWA）
• 判断：群内主流观点
• 标的：`$XXX`、`$XXX`
• 催化：推动该叙事的事件或数据

---

#### 💡 高质量观点精选

3-5 条最有价值的带论据分析，保留发言人：

**@发言人**
• 论点：核心主张
• 论据：链上数据 / 市场结构 / 事件逻辑

---

#### 🐋 链上与大户动向

按类别列出，无内容的类别省略：

**大户异动：** 具体行为描述
**资金流向：** 交易所净流入 / 流出
**合约持仓：** 多空比、爆仓数据

---

#### ⚠️ 风险提示

按类别列出，无内容的类别省略：

**监管风险：** 政策或事件
**项目风险：** `$代码` - 具体风险
**解锁抛压：** `$代码` - 时间与数量
**市场情绪：** 恐慌 / 过热信号

### 注意事项
- 区分"认真分析"和"随口闲聊"，重点提取前者
- 4 分以下的信号不要出现在输出中
- 保留发言人名称
- 注意币圈黑话和缩写（大饼=BTC、姨太=ETH、狗子=DOGE、SOL=梭了等）
- 合约交易要标注杠杆倍数
- 如有多空辩论，两方观点都要呈现
- 使用中文回复，用 Markdown 格式，直接输出分析内容"""

# 所有模板的注册表：模板 ID → (名称, 描述, prompt 文本)
ANALYSIS_TEMPLATES = {
    "general": {
        "name": "💬 通用群聊摘要",
        "description": "通用话题提取、观点汇总、氛围感知",
        "prompt": PROMPT_GENERAL,
    },
    "us_stock": {
        "name": "📊 美股行情追踪",
        "description": "识别美股标的、提取操作信号、分析多空观点",
        "prompt": PROMPT_US_STOCK,
    },
    "crypto": {
        "name": "🪙 加密货币追踪",
        "description": "识别币种、提取合约/现货信号、链上动向分析",
        "prompt": PROMPT_CRYPTO,
    },
    "custom": {
        "name": "✏️ 自定义 Prompt",
        "description": "使用你自己编写的分析指令",
        "prompt": "",  # 由用户填写
    },
}

# 默认 System Prompt（保持向后兼容）
SYSTEM_PROMPT = PROMPT_GENERAL


# ──────────────────────────────────────────────
# 智能采样：从大量消息中选取最有信息量的内容
# ──────────────────────────────────────────────

def _sample_messages(
    messages: List[TelegramMessage],
    max_count: int = MAX_SAMPLE_MESSAGES,
) -> List[TelegramMessage]:
    """
    从消息列表中智能采样，优先保留信息量大的消息。

    策略：
    - 过滤掉纯表情、极短消息（<5 字符）
    - 如果数量仍超限，均匀间隔采样（保留首尾 + 均匀抽取中间部分）
    """
    # 过滤低信息量消息
    useful = [
        m for m in messages
        if len(m.text.strip()) >= 5  # 至少 5 个字符
        and not m.text.strip().startswith("[")  # 跳过 [图片] [贴纸] 等系统消息
    ]

    # 不超限，直接返回
    if len(useful) <= max_count:
        return useful

    # 均匀采样
    step = len(useful) / max_count
    sampled = []
    for i in range(max_count):
        idx = int(i * step)
        sampled.append(useful[idx])

    return sampled


def _format_messages_as_json(
    messages: List[TelegramMessage],
    chat_name: str,
    start: str,
    end: str,
    total_count: int = 0,
    topic_name: Optional[str] = None,
) -> str:
    """
    将消息格式化为结构化 JSON，便于 AI 解析。

    包含元信息（群名、话题、时间范围、总条数/采样条数）和每条消息的
    时间、作者、内容、话题 ID、回复目标等上下文信息。
    同时写入临时文件供调试检查。
    """
    # 构建结构化数据
    data = {
        "chat_name": chat_name,
        "topic_name": topic_name,  # None 表示全部话题
        "time_range": f"{start} ~ {end}",
        "total_messages": total_count or len(messages),
        "sampled_messages": len(messages),
        "messages": [
            {
                "time": m.date.strftime("%m-%d %H:%M"),
                "author": m.author.replace("\x00", ""),
                "text": m.text.replace("\x00", ""),
                "topic_id": m.topic_id,
                "reply_to": m.reply_to_message_id,
            }
            for m in messages
        ],
    }

    # 序列化为 JSON 字符串
    json_str = json.dumps(data, ensure_ascii=False, indent=None)

    # 写入临时文件供调试检查（不阻塞主流程）
    try:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", prefix="tg_summary_",
            dir="/tmp", delete=False, encoding="utf-8",
        )
        tmp.write(json.dumps(data, ensure_ascii=False, indent=2))
        tmp.close()
        logger.info("消息 JSON 已写入临时文件: %s", tmp.name)
    except Exception:
        logger.debug("写入临时 JSON 文件失败（非关键错误）", exc_info=True)

    return json_str


# 保留旧函数兼容（统计摘要等可能还在用）
def _format_messages_for_ai(
    messages: List[TelegramMessage],
    chat_name: str,
    start: str,
    end: str,
) -> str:
    """将消息格式化为 AI 可读的文本（旧格式，保留兼容）。"""
    lines = [
        f"群组名称: {chat_name}",
        f"时间范围: {start} ~ {end}",
        f"消息总数: {len(messages)} 条（以下为采样）",
        "",
        "--- 聊天记录开始 ---",
    ]

    for m in messages:
        time_str = m.date.strftime("%m-%d %H:%M")
        text = m.text.replace("\x00", "")
        author = m.author.replace("\x00", "")
        lines.append(f"[{time_str}] {author}: {text}")

    lines.append("--- 聊天记录结束 ---")
    return "\n".join(lines)


# ──────────────────────────────────────────────
# 查找 CLI 工具
# ──────────────────────────────────────────────

def _find_cli(name: str) -> Optional[str]:
    """查找指定 CLI 可执行文件路径。"""
    candidates = [
        f"/usr/local/bin/{name}",        # 常见安装路径
        f"/opt/homebrew/bin/{name}",      # Apple Silicon Homebrew
        shutil.which(name),              # PATH 中查找
    ]
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


# ──────────────────────────────────────────────
# 方式一：通过 claude CLI 调用
# ──────────────────────────────────────────────

async def _call_claude_cli(
    prompt: str,
    timeout: int = CLI_TIMEOUT,
    model: Optional[str] = None,
) -> str:
    """
    调用 claude CLI 的 -p 模式（单次问答，非交互）。

    关键改进：
    - 通过 stdin 传递 prompt（避免命令行参数长度限制，大量消息时更可靠）
    - 支持指定模型（如 haiku 加速分析）
    - 清除 CLAUDECODE 环境变量，避免嵌套检测
    """
    claude_path = _find_cli("claude")
    if not claude_path:
        raise FileNotFoundError("找不到 claude CLI")

    # 移除 CLAUDECODE 避免嵌套限制
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

    # 构建命令参数：-p 不带 prompt 参数时，从 stdin 读取输入
    cmd = [claude_path, "-p", "--output-format", "text"]
    # 如果指定了模型，添加 --model 参数
    if model:
        cmd.extend(["--model", model])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,   # 通过 stdin 传递 prompt
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    try:
        # 将 prompt 写入 stdin，然后等待输出
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=prompt.encode("utf-8")),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise TimeoutError(f"claude CLI 超时（{timeout}秒）")

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"claude CLI 错误 (code={proc.returncode}): {err}")

    result = stdout.decode("utf-8", errors="replace").strip()
    if not result:
        raise RuntimeError("claude CLI 返回空结果")

    return result


# ──────────────────────────────────────────────
# 方式二：通过 codex CLI 调用（备选）
# ──────────────────────────────────────────────

async def _call_codex_cli(prompt: str, timeout: int = CLI_TIMEOUT) -> str:
    """
    调用 codex exec 模式（非交互）。

    codex exec --ephemeral --skip-git-repo-check -o output.txt "prompt"
    通过 -o 参数将最终回复写入临时文件，然后读取。
    """
    codex_path = _find_cli("codex")
    if not codex_path:
        raise FileNotFoundError("找不到 codex CLI")

    # 创建临时文件接收 codex 输出
    tmp_out = tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", prefix="codex_out_", delete=False
    )
    tmp_out.close()

    try:
        proc = await asyncio.create_subprocess_exec(
            codex_path, "exec",
            "--ephemeral",               # 不持久化会话
            "--skip-git-repo-check",     # 无需 git 仓库
            "-o", tmp_out.name,          # 最终回复写入文件
            prompt,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise TimeoutError(f"codex CLI 超时（{timeout}秒）")

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"codex CLI 错误 (code={proc.returncode}): {err}")

        # 读取输出文件
        with open(tmp_out.name, "r", encoding="utf-8") as f:
            result = f.read().strip()

        if not result:
            # 如果 -o 文件为空，尝试从 stdout 获取
            result = stdout.decode("utf-8", errors="replace").strip()

        if not result:
            raise RuntimeError("codex CLI 返回空结果")

        return result

    finally:
        # 清理临时文件
        try:
            os.unlink(tmp_out.name)
        except OSError:
            pass


# ──────────────────────────────────────────────
# 公开 API
# ──────────────────────────────────────────────

def check_ai_available() -> dict:
    """
    检查 AI 功能可用性，返回状态信息。

    Returns:
        {
            "available": True/False,
            "method": "claude_cli" | "codex_cli" | None,
            "detail": "描述信息"
        }
    """
    # 优先检查 claude CLI
    claude = _find_cli("claude")
    if claude:
        return {
            "available": True,
            "method": "claude_cli",
            "detail": f"使用本地 Claude Code CLI ({claude})",
        }

    # 备选：codex CLI
    codex = _find_cli("codex")
    if codex:
        return {
            "available": True,
            "method": "codex_cli",
            "detail": f"使用本地 Codex CLI ({codex})",
        }

    return {
        "available": False,
        "method": None,
        "detail": "未检测到 Claude Code 或 Codex CLI。请安装其中之一。",
    }


async def generate_ai_summary(
    messages: List[TelegramMessage],
    chat_name: str,
    start: str,
    end: str,
    method: Optional[str] = None,
    system_prompt: Optional[str] = None,
    max_sample: Optional[int] = None,
    cli_timeout: Optional[int] = None,
    cli_model: Optional[str] = None,
    topic_name: Optional[str] = None,
) -> str:
    """
    使用 AI 生成群聊智能摘要。

    Args:
        messages: 已按时间排序的消息列表
        chat_name: 群组名称
        start: 开始时间字符串
        end: 结束时间字符串
        method: 强制指定方法（"claude_cli" | "codex_cli" | "auto"），None 则自动选择
        system_prompt: 自定义 System Prompt，None 则使用默认
        max_sample: 最大采样消息数，None 则使用默认 (500)
        cli_timeout: CLI 超时秒数，None 则使用默认 (300)
        cli_model: 指定模型（如 "claude-haiku-4-5-20251001"），None 则使用 CLI 默认
        topic_name: 当前分析的话题名称，None 表示全部话题

    Returns:
        AI 生成的 Markdown 格式摘要文本
    """
    # 使用传入的配置或默认值
    _prompt = system_prompt or SYSTEM_PROMPT
    _max_sample = max_sample or MAX_SAMPLE_MESSAGES
    _timeout = cli_timeout or CLI_TIMEOUT
    _model = cli_model or CLI_MODEL  # None 表示使用 CLI 默认模型

    # 记录原始消息总数（采样前）
    total_count = len(messages)

    # 智能采样
    sampled = _sample_messages(messages, max_count=_max_sample)

    # 格式化为结构化 JSON（包含 topic_id、reply_to 等上下文）
    chat_json = _format_messages_as_json(
        sampled, chat_name, start, end,
        total_count=total_count,
        topic_name=topic_name,
    )

    # 组合 prompt：分析指令 + JSON 数据
    full_prompt = f"{_prompt}\n\n以下是结构化 JSON 格式的聊天记录数据：\n{chat_json}"

    # 自动选择（method=None 或 "auto" 都走自动检测逻辑）
    if method is None or method == "auto":
        status = check_ai_available()
        if not status["available"]:
            raise RuntimeError(status["detail"])
        method = status["method"]

    # 调用对应的 CLI
    if method == "claude_cli":
        return await _call_claude_cli(full_prompt, timeout=_timeout, model=_model)
    elif method == "codex_cli":
        return await _call_codex_cli(full_prompt, timeout=_timeout)
    else:
        raise ValueError(f"未知的 AI 方法: {method}")
