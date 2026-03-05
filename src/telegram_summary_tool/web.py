"""
Web 后台服务：提供群列表浏览、搜索、一键生成摘要的可视化界面。

启动方式：
    source .venv/bin/activate
    python -m telegram_summary_tool.web
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

import threading

# 配置日志级别，确保能看到性能日志
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")


from .ai_summarizer import (
    check_ai_available,
    generate_ai_summary,
    SYSTEM_PROMPT,
    MAX_SAMPLE_MESSAGES,
    CLI_TIMEOUT,
    CLI_MODEL,
    ANALYSIS_TEMPLATES,
)
from .local_db import list_chats, load_messages_from_local, warmup_cache, refresh_cache
from .summarizer import build_summary_report, filter_messages_by_range

app = FastAPI(title="Telegram Summary Tool")
logger = logging.getLogger(__name__)


@app.on_event("startup")
def on_startup():
    """服务启动时后台预热数据库缓存，用户打开页面时数据已就绪。"""
    threading.Thread(target=warmup_cache, daemon=True).start()


# ──────────────────────────────────────────────
# API 路由
# ──────────────────────────────────────────────

@app.get("/api/chats")
def api_chats():
    """返回所有群组/频道列表"""
    try:
        chats = list_chats(min_messages=1)
        return [
            {
                "peer_id": c.peer_id,
                "peer_type": c.peer_type,
                "title": c.title,
                "username": c.username,
                "message_count": c.message_count,
            }
            for c in chats
        ]
    except Exception as exc:
        logger.exception("加载群组列表失败")
        raise HTTPException(status_code=500, detail=f"加载群组列表失败: {exc}") from exc


def _load_messages_with_peer_fallback(chat_id: int, peer_type: int):
    """
    按 peer_type 读取消息；读取不到时在群/频道间兜底一次。

    目的：
    - 兼容前端未传 peer_type 的旧请求
    - 避免把普通群(type=1)误当频道(type=2)导致"没有消息"

    返回: (chat_name, messages, used_peer_type, topics)
    """
    peer_candidates: list[int] = []
    if peer_type in (1, 2):
        peer_candidates.append(peer_type)
    peer_candidates.extend([t for t in (2, 1) if t not in peer_candidates])

    chat_name = "Unknown Chat"
    messages = []
    topics = {}
    used_peer_type = peer_candidates[0]
    for candidate in peer_candidates:
        used_peer_type = candidate
        chat_name, messages, topics = load_messages_from_local(peer_id=chat_id, peer_type=candidate)
        if messages:
            if candidate != peer_type:
                logger.info(
                    "chat_id=%s peer_type=%s 无消息，已回退到 peer_type=%s 成功读取 %d 条",
                    chat_id, peer_type, candidate, len(messages),
                )
            break
    return chat_name, messages, used_peer_type, topics


@app.post("/api/refresh")
def api_refresh():
    """强制刷新本地数据库缓存（重新解密导出，约 15 秒）"""
    try:
        refresh_cache()
        return {"ok": True, "message": "数据库缓存已刷新"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


# ──────────────────────────────────────────────
# AI 配置（运行时可修改，重启恢复默认）
# ──────────────────────────────────────────────

_ai_config = {
    "method": "auto",                            # auto / claude_cli / codex_cli
    "template": "general",                       # 分析模板 ID（general/us_stock/crypto/custom）
    "system_prompt": SYSTEM_PROMPT,              # AI 分析的 System Prompt（由模板决定或用户自定义）
    "max_sample_messages": MAX_SAMPLE_MESSAGES,  # 发送给 AI 的最大消息条数
    "cli_timeout": CLI_TIMEOUT,                  # CLI 超时时间（秒）
    "cli_model": CLI_MODEL or "",                # 模型选择（空 = CLI 默认）
}


@app.get("/api/ai-templates")
def api_get_templates():
    """返回所有可用的分析模板（不含 prompt 全文，减少传输量）"""
    return [
        {"id": tid, "name": t["name"], "description": t["description"]}
        for tid, t in ANALYSIS_TEMPLATES.items()
    ]


@app.get("/api/ai-config")
def api_get_ai_config():
    """获取当前 AI 配置"""
    return _ai_config


@app.post("/api/ai-config")
async def api_set_ai_config(request: Request):
    """更新 AI 配置"""
    body = await request.json()
    if "method" in body and body["method"] in ("auto", "claude_cli", "codex_cli"):
        _ai_config["method"] = body["method"]
    # 模板切换：选非 custom 模板时自动设置对应 prompt
    if "template" in body and body["template"] in ANALYSIS_TEMPLATES:
        _ai_config["template"] = body["template"]
        if body["template"] != "custom":
            # 非自定义模板 → 用模板内置 prompt 覆盖
            _ai_config["system_prompt"] = ANALYSIS_TEMPLATES[body["template"]]["prompt"]
    # 仅当 custom 模板时才接受用户手写 prompt
    if _ai_config["template"] == "custom":
        if "system_prompt" in body and isinstance(body["system_prompt"], str) and body["system_prompt"].strip():
            _ai_config["system_prompt"] = body["system_prompt"].strip()
    if "max_sample_messages" in body:
        val = int(body["max_sample_messages"])
        _ai_config["max_sample_messages"] = max(50, min(2000, val))
    if "cli_timeout" in body:
        val = int(body["cli_timeout"])
        _ai_config["cli_timeout"] = max(30, min(600, val))
    if "cli_model" in body and isinstance(body["cli_model"], str):
        # 空字符串表示使用 CLI 默认模型
        _ai_config["cli_model"] = body["cli_model"].strip()
    return {"ok": True, "config": _ai_config}


@app.post("/api/ai-config/reset")
def api_reset_ai_config():
    """重置 AI 配置为默认值"""
    _ai_config["method"] = "auto"
    _ai_config["template"] = "general"
    _ai_config["system_prompt"] = SYSTEM_PROMPT
    _ai_config["max_sample_messages"] = MAX_SAMPLE_MESSAGES
    _ai_config["cli_timeout"] = CLI_TIMEOUT
    _ai_config["cli_model"] = CLI_MODEL or ""
    return {"ok": True, "config": _ai_config}


@app.get("/api/messages")
def api_messages(
    chat_id: int = Query(..., description="群组 peer_id"),
    peer_type: int = Query(2, description="peer 类型：2=Channel, 1=Group"),
    days: int = Query(7, description="回溯天数"),
    start: str = Query(None, description="开始日期 YYYY-MM-DD"),
    end: str = Query(None, description="结束日期 YYYY-MM-DD"),
    limit: int = Query(200, description="最多返回消息条数"),
    topic_id: int = Query(None, description="按话题 ID 过滤（仅 Forum 群组）"),
):
    """返回指定群组在日期范围内的聊天记录（用于预览）"""
    chat_name, messages, used_peer_type, topics = _load_messages_with_peer_fallback(
        chat_id=chat_id,
        peer_type=peer_type,
    )
    if not messages:
        return {"error": "该群没有可解析的消息", "messages": [], "chat_name": "", "topics": {}}

    # 计算时间范围
    last_time = messages[-1].date
    end_dt = datetime.strptime(end, "%Y-%m-%d").replace(hour=23, minute=59, second=59) if end else last_time
    start_dt = datetime.strptime(start, "%Y-%m-%d") if start else end_dt - timedelta(days=max(1, days))

    selected = filter_messages_by_range(messages, start_dt, end_dt)
    if not selected:
        return {
            "error": f"在 {start_dt.date()} ~ {end_dt.date()} 范围内没有消息",
            "messages": [],
            "chat_name": chat_name,
            "topics": topics,
        }

    # 按话题过滤（如果指定了 topic_id）
    if topic_id is not None:
        selected = [m for m in selected if m.topic_id == topic_id]

    # 取最后 limit 条（最新的消息）
    tail = selected[-limit:]
    return {
        "chat_name": chat_name,
        "peer_type": used_peer_type,
        "total_count": len(selected),
        "returned_count": len(tail),
        "start": start_dt.strftime("%Y-%m-%d"),
        "end": end_dt.strftime("%Y-%m-%d"),
        "topics": topics,
        "messages": [
            {
                "id": m.message_id,
                "date": m.date.strftime("%m-%d %H:%M"),
                "author": m.author.replace("\x00", ""),
                "text": m.text.replace("\x00", "")[:500],  # 清除空字节并截断
                "reply_to": m.reply_to_message_id,
                "topic_id": m.topic_id,
            }
            for m in tail
        ],
    }


@app.get("/api/summary")
def api_summary(
    chat_id: int = Query(..., description="群组 peer_id"),
    peer_type: int = Query(2, description="peer 类型：2=Channel, 1=Group"),
    days: int = Query(7, description="回溯天数"),
    start: str = Query(None, description="开始日期 YYYY-MM-DD"),
    end: str = Query(None, description="结束日期 YYYY-MM-DD"),
    top_users: int = Query(15),
    top_keywords: int = Query(25),
    max_actions: int = Query(15),
    topic_id: int = Query(None, description="按话题 ID 过滤"),
):
    """生成指定群组的摘要"""
    chat_name, messages, used_peer_type, topics = _load_messages_with_peer_fallback(
        chat_id=chat_id,
        peer_type=peer_type,
    )

    if not messages:
        return {"error": "该群没有可解析的消息"}

    # 计算时间范围
    last_time = messages[-1].date
    end_dt = datetime.strptime(end, "%Y-%m-%d").replace(hour=23, minute=59, second=59) if end else last_time
    start_dt = datetime.strptime(start, "%Y-%m-%d") if start else end_dt - timedelta(days=max(1, days))

    selected = filter_messages_by_range(messages, start_dt, end_dt)
    if not selected:
        return {"error": f"在 {start_dt.date()} ~ {end_dt.date()} 范围内没有消息"}

    # 按话题过滤
    if topic_id is not None:
        selected = [m for m in selected if m.topic_id == topic_id]
        if not selected:
            return {"error": f"话题 {topic_id} 在该时间范围内没有消息"}

    report = build_summary_report(
        chat_name=chat_name,
        messages=selected,
        start=start_dt,
        end=end_dt,
        top_users=top_users,
        top_keywords=top_keywords,
        max_actions=max_actions,
        topics=topics,
    )

    return {
        "chat_name": report.chat_name,
        "peer_type": used_peer_type,
        "start": report.start.strftime("%Y-%m-%d %H:%M"),
        "end": report.end.strftime("%Y-%m-%d %H:%M"),
        "message_count": report.message_count,
        "active_user_count": report.active_user_count,
        "top_users": [{"name": n, "count": c} for n, c in report.top_users],
        "top_keywords": [{"word": w, "count": c} for w, c in report.top_keywords],
        "action_items": report.action_items,
        "hot_replies": report.hot_replies,
        "hourly_activity": [{"hour": h, "count": c} for h, c in report.hourly_activity],
        "topics": topics,
        "topic_stats": [
            {
                "topic_id": ts.topic_id,
                "topic_name": ts.topic_name,
                "message_count": ts.message_count,
                "active_users": ts.active_users,
                "top_users": [{"name": n, "count": c} for n, c in ts.top_users],
                "top_keywords": [{"word": w, "count": c} for w, c in ts.top_keywords],
            }
            for ts in report.topic_stats
        ],
    }


@app.get("/api/ai-status")
def api_ai_status():
    """检查 AI 功能是否可用"""
    return check_ai_available()


@app.get("/api/ai-summary")
async def api_ai_summary(
    chat_id: int = Query(..., description="群组 peer_id"),
    peer_type: int = Query(2, description="peer 类型：2=Channel, 1=Group"),
    days: int = Query(7, description="回溯天数"),
    start: str = Query(None, description="开始日期 YYYY-MM-DD"),
    end: str = Query(None, description="结束日期 YYYY-MM-DD"),
    topic_id: int = Query(None, description="按话题 ID 过滤（仅 Forum 群组）"),
):
    """调用 AI 生成智能摘要，支持按话题过滤"""
    # 检查 AI 是否可用
    status = check_ai_available()
    if not status["available"]:
        return {"error": status["detail"]}

    # 加载消息
    chat_name, messages, used_peer_type, _topics = _load_messages_with_peer_fallback(
        chat_id=chat_id,
        peer_type=peer_type,
    )
    if not messages:
        return {"error": "该群没有可解析的消息"}

    # 计算时间范围
    last_time = messages[-1].date
    end_dt = datetime.strptime(end, "%Y-%m-%d").replace(hour=23, minute=59, second=59) if end else last_time
    start_dt = datetime.strptime(start, "%Y-%m-%d") if start else end_dt - timedelta(days=max(1, days))

    selected = filter_messages_by_range(messages, start_dt, end_dt)
    if not selected:
        return {"error": f"在 {start_dt.date()} ~ {end_dt.date()} 范围内没有消息"}

    # 按话题过滤（如果指定了 topic_id）
    topic_name = None
    if topic_id is not None:
        # 从 topics 字典中获取话题名称
        topic_name = _topics.get(topic_id, f"话题#{topic_id}")
        selected = [m for m in selected if m.topic_id == topic_id]
        if not selected:
            return {"error": f"话题「{topic_name}」在该时间范围内没有消息"}

    # 调用 AI 分析（使用页面配置的参数）
    try:
        ai_text = await generate_ai_summary(
            messages=selected,
            chat_name=chat_name,
            start=start_dt.strftime("%Y-%m-%d %H:%M"),
            end=end_dt.strftime("%Y-%m-%d %H:%M"),
            method=_ai_config["method"],
            system_prompt=_ai_config["system_prompt"],
            max_sample=_ai_config["max_sample_messages"],
            cli_timeout=_ai_config["cli_timeout"],
            cli_model=_ai_config["cli_model"] or None,
            topic_name=topic_name,
        )
        actual_method = _ai_config["method"] if _ai_config["method"] != "auto" else status["method"]
        return {
            "ai_summary": ai_text,
            "method": actual_method,
            "peer_type": used_peer_type,
            "message_count": len(selected),
            "topic_name": topic_name,
        }
    except Exception as e:
        return {"error": f"AI 分析失败: {str(e)}"}


# ──────────────────────────────────────────────
# 前端页面
# ──────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index():
    # 设置 CSP 头：本地工具，放宽策略以兼容浏览器扩展和调试工具
    return HTMLResponse(
        content=FRONTEND_HTML,
        headers={
            "Content-Security-Policy": (
                "default-src 'self'; "
                "script-src 'unsafe-inline' 'unsafe-eval'; "  # 允许内联脚本 + eval（兼容调试/扩展）
                "style-src 'unsafe-inline'; "
                "connect-src 'self'"  # 明确允许同源 fetch
            ),
        },
    )


FRONTEND_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Telegram Summary Tool</title>
<style>
  /* ===== 全局重置 & 基础 ===== */
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0D1117; color: #E6EDF3; min-height: 100vh;
  }

  /* ===== 顶部导航栏 ===== */
  .header {
    background: #161B22; border-bottom: 1px solid #21262D;
    padding: 0 20px; display: flex; align-items: center; height: 56px;
  }
  /* Logo 区域：紫色竖条 + 标题 */
  .header .logo {
    display: flex; align-items: center; gap: 10px; margin-right: 16px;
  }
  .header .logo-bar {
    width: 4px; height: 24px; background: #7C3AED; border-radius: 2px;
  }
  .header .logo h1 { font-size: 16px; font-weight: 600; color: #E6EDF3; }
  .header .logo .subtitle { font-size: 11px; color: #484F58; margin-left: 8px; }
  /* 中部导航标签 */
  .header .nav-tabs {
    display: flex; gap: 4px; margin: 0 auto;
  }
  .header .nav-tab {
    padding: 8px 16px; background: transparent; border: 1px solid transparent;
    border-radius: 6px; color: #8B949E; font-size: 13px; cursor: pointer;
    transition: all 0.2s;
  }
  .header .nav-tab:hover { color: #C9D1D9; background: #1C2333; }
  .header .nav-tab.active { color: #E6EDF3; background: #1F6FEB; border-color: #1F6FEB; }
  /* 右侧操作区 */
  .header .header-actions { display: flex; align-items: center; gap: 8px; }

  /* ===== 主布局：三栏 ===== */
  .layout { display: flex; height: calc(100vh - 56px); }

  /* ===== 按钮通用 ===== */
  .btn {
    padding: 8px 16px; background: #1F6FEB; color: #fff; border: none;
    border-radius: 6px; font-size: 13px; font-weight: 500; cursor: pointer;
    transition: all 0.2s; white-space: nowrap;
  }
  .btn:hover { background: #58A6FF; }
  .btn:disabled { background: #21262D; color: #484F58; cursor: not-allowed; }
  .btn-purple { background: #7C3AED; }
  .btn-purple:hover { background: #A78BFA; }
  .btn-outline {
    padding: 6px 14px; background: transparent; border: 1px solid #30363D;
    border-radius: 6px; color: #8B949E; font-size: 12px; cursor: pointer;
    transition: all 0.2s;
  }
  .btn-outline:hover { border-color: #58A6FF; color: #E6EDF3; }

  /* ===== 左栏：群组列表 ===== */
  .left-panel {
    width: 260px; min-width: 260px; background: #0F1319;
    border-right: 1px solid #21262D; display: flex; flex-direction: column;
  }
  /* 搜索栏 */
  .left-panel .search-box {
    padding: 12px 14px; border-bottom: 1px solid #21262D;
  }
  .left-panel .search-box input {
    width: 100%; padding: 8px 12px; background: #161B22; border: 1px solid #30363D;
    border-radius: 6px; color: #E6EDF3; font-size: 13px; outline: none;
  }
  .left-panel .search-box input::placeholder { color: #484F58; }
  .left-panel .search-box input:focus { border-color: #58A6FF; }
  /* 群组列表区域 */
  .left-panel .chat-list { flex: 1; overflow-y: auto; }
  .left-panel .section-label {
    font-size: 11px; color: #484F58; letter-spacing: 0.5px; font-weight: 600;
    padding: 12px 14px 6px; display: flex; align-items: center; gap: 6px;
  }
  /* 群组条目 */
  .chat-item {
    display: flex; align-items: center; gap: 10px; padding: 10px 14px;
    cursor: pointer; transition: background 0.15s; border-left: 3px solid transparent;
  }
  .chat-item:hover { background: #161B22; }
  .chat-item.active { background: #1C2333; border-left-color: #1F6FEB; }
  /* 彩色头像圆圈 */
  .chat-avatar {
    width: 36px; height: 36px; border-radius: 8px; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center;
    font-size: 14px; font-weight: 700; color: #fff;
  }
  .chat-item .chat-info { flex: 1; min-width: 0; }
  .chat-item .chat-title {
    font-size: 13px; font-weight: 500; color: #E6EDF3;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .chat-item .chat-meta { font-size: 11px; color: #484F58; margin-top: 2px; }
  .chat-item .chat-status {
    font-size: 10px; padding: 2px 8px; border-radius: 10px;
    background: #1C2333; color: #8B949E; flex-shrink: 0;
  }
  .chat-item.active .chat-status { background: #1F6FEB33; color: #58A6FF; }
  /* 底部按钮 */
  .left-panel .panel-footer {
    padding: 12px 14px; border-top: 1px solid #21262D;
  }
  .left-panel .empty-hint { color: #484F58; font-size: 13px; text-align: center; padding: 40px 14px; }

  /* ===== 中栏：消息 & 摘要 ===== */
  .center-panel {
    flex: 1; display: flex; flex-direction: column; overflow: hidden;
    background: #0D1117;
  }
  /* 中栏顶部：群名 + 日期 + 操作 */
  .center-header {
    padding: 14px 20px; border-bottom: 1px solid #21262D; background: #0F1319;
    display: flex; flex-direction: column; gap: 10px;
  }
  .center-header .center-title-row {
    display: flex; align-items: center; justify-content: space-between;
  }
  .center-header .center-title { font-size: 16px; font-weight: 600; color: #E6EDF3; }
  .center-header .center-date { font-size: 12px; color: #484F58; }
  .center-header .center-stats { font-size: 12px; color: #8B949E; }
  /* 控制栏（日期+按钮） */
  .center-controls {
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  }
  .center-controls label { font-size: 12px; color: #8B949E; }
  .center-controls input[type="date"],
  .center-controls select {
    padding: 6px 10px; background: #161B22; border: 1px solid #30363D;
    border-radius: 6px; color: #E6EDF3; font-size: 12px; outline: none;
    color-scheme: dark;
  }
  .center-controls input:focus, .center-controls select:focus { border-color: #58A6FF; }
  /* 话题标签栏 */
  .topic-bar {
    display: flex; gap: 6px; flex-wrap: wrap; padding: 0;
  }
  .topic-tab {
    padding: 6px 14px; background: #161B22; border: 1px solid #30363D;
    border-radius: 16px; font-size: 12px; color: #8B949E; cursor: pointer;
    transition: all 0.2s; white-space: nowrap;
  }
  .topic-tab:hover { border-color: #58A6FF; color: #C9D1D9; }
  .topic-tab.active { background: #1F6FEB; border-color: #1F6FEB; color: #fff; font-weight: 500; }
  .topic-tab .topic-count {
    display: inline-block; background: rgba(255,255,255,0.15); padding: 0 6px;
    border-radius: 8px; font-size: 10px; margin-left: 4px;
  }
  /* 内容滚动区 */
  .center-content { flex: 1; overflow-y: auto; padding: 16px 20px; }
  .center-content .empty {
    display: flex; align-items: center; justify-content: center;
    height: 100%; color: #484F58; font-size: 14px;
  }

  /* ===== 卡片 ===== */
  .card {
    background: #161B22; border: 1px solid #21262D; border-radius: 10px;
    padding: 16px; margin-bottom: 12px;
  }
  .card h2 {
    font-size: 14px; color: #58A6FF; margin-bottom: 12px;
    padding-bottom: 8px; border-bottom: 1px solid #21262D;
  }

  /* ===== 统计数字 ===== */
  .stats { display: flex; gap: 12px; margin-bottom: 12px; }
  .stat-box {
    flex: 1; background: #161B22; border: 1px solid #21262D;
    border-radius: 8px; padding: 14px; text-align: center;
  }
  .stat-box .number { font-size: 24px; font-weight: 700; color: #58A6FF; }
  .stat-box .label { font-size: 11px; color: #8B949E; margin-top: 4px; }

  /* ===== 用户排行 ===== */
  .user-bar { display: flex; align-items: center; margin-bottom: 8px; gap: 10px; }
  .user-bar .name { width: 120px; font-size: 13px; text-align: right; color: #C9D1D9; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .user-bar .bar-bg { flex: 1; background: #21262D; border-radius: 4px; height: 20px; overflow: hidden; }
  .user-bar .bar-fill { height: 100%; background: linear-gradient(90deg, #1F6FEB, #58A6FF); border-radius: 4px; min-width: 2px; transition: width 0.5s; }
  .user-bar .count { width: 40px; font-size: 12px; color: #8B949E; }

  /* ===== 关键词标签 ===== */
  .keywords { display: flex; flex-wrap: wrap; gap: 6px; }
  .keyword {
    background: #1C2333; padding: 4px 12px; border-radius: 16px;
    font-size: 12px; border: 1px solid #30363D;
  }
  .keyword .w { color: #58A6FF; }
  .keyword .c { color: #484F58; font-size: 10px; margin-left: 4px; }

  /* ===== 行动项 ===== */
  .action-item {
    padding: 8px 0; border-bottom: 1px solid #21262D; font-size: 13px; line-height: 1.6; color: #C9D1D9;
  }
  .action-item:last-child { border-bottom: none; }

  /* ===== 活跃度图表 ===== */
  .hour-chart { display: flex; align-items: flex-end; gap: 3px; height: 100px; padding-top: 8px; }
  .hour-bar-wrap { flex: 1; display: flex; flex-direction: column; align-items: center; height: 100%; justify-content: flex-end; }
  .hour-bar {
    width: 100%; background: linear-gradient(0deg, #1F6FEB, #58A6FF);
    border-radius: 2px 2px 0 0; min-height: 2px; transition: height 0.5s;
  }
  .hour-label { font-size: 9px; color: #484F58; margin-top: 3px; }

  /* ===== 消息列表 ===== */
  .msg-section-header {
    display: flex; align-items: center; gap: 8px; padding: 10px 0 6px;
    font-size: 13px; color: #8B949E; border-bottom: 1px solid #21262D; margin-bottom: 4px;
  }
  .msg-section-header .section-icon { color: #58A6FF; }
  .msg-list { display: flex; flex-direction: column; gap: 1px; }
  .msg-item {
    display: flex; gap: 10px; padding: 8px 10px; border-radius: 6px;
    font-size: 13px; line-height: 1.5; transition: background 0.15s;
    align-items: flex-start;
  }
  .msg-item:hover { background: #161B22; }
  /* 消息头像 */
  .msg-avatar {
    width: 32px; height: 32px; border-radius: 6px; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center;
    font-size: 12px; font-weight: 700; color: #fff; margin-top: 2px;
  }
  .msg-item .msg-body { flex: 1; min-width: 0; }
  .msg-item .msg-author {
    font-size: 13px; font-weight: 500; margin-right: 8px;
  }
  .msg-item .msg-text { color: #C9D1D9; word-break: break-word; }
  .msg-item .msg-time { color: #484F58; font-size: 11px; white-space: nowrap; flex-shrink: 0; margin-top: 3px; }
  .msg-item .msg-reply-tag {
    font-size: 10px; color: #8B949E; background: #1C2333; padding: 1px 6px;
    border-radius: 4px; margin-right: 4px;
  }

  /* ===== 右栏：AI 分析 ===== */
  .right-panel {
    width: 320px; min-width: 320px; background: #0F1319;
    border-left: 1px solid #21262D; display: flex; flex-direction: column;
  }
  /* 右栏头部 */
  .right-header {
    padding: 14px 16px; border-bottom: 1px solid #21262D;
  }
  .right-header h2 {
    font-size: 14px; font-weight: 600; color: #E6EDF3;
    display: flex; align-items: center; gap: 6px;
  }
  .right-header .ai-sub { font-size: 11px; color: #484F58; margin-top: 2px; }
  /* 右栏内容 */
  .right-content { flex: 1; overflow-y: auto; padding: 16px; }
  .right-panel .empty-hint { color: #484F58; font-size: 13px; text-align: center; padding: 40px 16px; }
  /* AI 分析区块 */
  .ai-section { margin-bottom: 20px; }
  .ai-section-title {
    font-size: 13px; font-weight: 600; color: #E6EDF3; margin-bottom: 10px;
    display: flex; align-items: center; gap: 6px;
  }
  .ai-section-title .icon { font-size: 14px; }
  .ai-section .ai-list { display: flex; flex-direction: column; gap: 6px; }
  .ai-section .ai-list-item {
    display: flex; align-items: center; gap: 8px; font-size: 12px; color: #C9D1D9;
  }
  .ai-section .ai-list-item .bullet { color: #58A6FF; flex-shrink: 0; }
  .ai-section .ai-list-item .count { color: #484F58; margin-left: auto; font-size: 11px; }
  /* 右栏用户条形图 */
  .ai-user-bar { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
  .ai-user-bar .rank { font-size: 12px; color: #484F58; width: 16px; text-align: right; }
  .ai-user-bar .name { font-size: 12px; color: #C9D1D9; width: 80px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ai-user-bar .bar-bg { flex: 1; background: #21262D; border-radius: 3px; height: 16px; overflow: hidden; }
  .ai-user-bar .bar-fill { height: 100%; border-radius: 3px; min-width: 2px; transition: width 0.5s; }
  .ai-user-bar .count { font-size: 11px; color: #8B949E; width: 30px; text-align: right; }
  /* 情绪分析条 */
  .emotion-bar-wrap { margin-top: 8px; }
  .emotion-bar {
    display: flex; height: 8px; border-radius: 4px; overflow: hidden; margin-bottom: 6px;
  }
  .emotion-bar .seg-positive { background: #238636; }
  .emotion-bar .seg-neutral { background: #F59E0B; }
  .emotion-bar .seg-negative { background: #F85149; }
  .emotion-labels { display: flex; gap: 12px; font-size: 11px; }
  .emotion-labels span { display: flex; align-items: center; gap: 4px; color: #8B949E; }
  .emotion-labels .dot { width: 8px; height: 8px; border-radius: 50%; }
  /* AI 摘要卡片（Markdown 渲染） */
  .ai-card {
    background: linear-gradient(135deg, #161B22 0%, #1C1528 100%);
    border: 1px solid #7C3AED44; border-radius: 10px;
    padding: 16px; margin-bottom: 12px;
  }
  .ai-card h2 { color: #A78BFA; font-size: 14px; border-bottom-color: #7C3AED44; }
  .ai-card .ai-content { font-size: 13px; line-height: 1.8; white-space: pre-wrap; color: #C9D1D9; }
  .ai-card .ai-content h3 { color: #A78BFA; margin: 14px 0 6px; font-size: 14px; }
  .ai-card .ai-content strong { color: #E6EDF3; }
  .ai-card .ai-content li { margin: 3px 0; }
  .ai-badge {
    display: inline-block; background: #7C3AED33; padding: 2px 8px;
    border-radius: 8px; font-size: 10px; color: #A78BFA; margin-left: 6px;
  }
  /* 热门关键词标签 */
  .ai-tags { display: flex; flex-wrap: wrap; gap: 6px; }
  .ai-tag {
    padding: 4px 10px; border-radius: 12px; font-size: 11px;
    border: 1px solid #30363D; cursor: default;
  }
  /* 右栏底部操作栏 */
  .right-footer {
    padding: 12px 16px; border-top: 1px solid #21262D;
    display: flex; gap: 8px; align-items: center;
  }

  /* ===== AI 配置面板 ===== */
  .config-toggle {
    padding: 6px 14px; background: #161B22; border: 1px solid #30363D;
    border-radius: 6px; color: #8B949E; cursor: pointer; font-size: 12px;
    transition: all 0.2s; display: flex; align-items: center; gap: 4px;
  }
  .config-toggle:hover { border-color: #7C3AED; color: #A78BFA; }
  .config-toggle.active { border-color: #7C3AED; color: #A78BFA; background: #1C1528; }
  .config-panel { max-height: 0; overflow: hidden; transition: max-height 0.4s ease; }
  .config-panel.open { max-height: 800px; }
  .config-inner { padding: 12px 0 0; }
  .config-row { display: flex; flex-direction: column; gap: 10px; margin-bottom: 12px; }
  .config-field { display: flex; flex-direction: column; gap: 4px; }
  .config-field label { font-size: 11px; color: #8B949E; font-weight: 500; }
  .config-field select,
  .config-field input[type="number"] {
    padding: 6px 10px; background: #161B22; border: 1px solid #30363D;
    border-radius: 6px; color: #E6EDF3; font-size: 12px; outline: none;
  }
  .config-field select:focus,
  .config-field input[type="number"]:focus { border-color: #7C3AED; }
  .template-section { margin-bottom: 12px; }
  .template-section > label { display: block; font-size: 11px; color: #8B949E; font-weight: 500; margin-bottom: 6px; }
  .template-list { display: flex; flex-direction: column; gap: 6px; }
  .template-card {
    display: flex; align-items: center; gap: 8px; padding: 8px 10px;
    background: #161B22; border: 1px solid #30363D; border-radius: 8px;
    cursor: pointer; transition: all 0.2s;
  }
  .template-card:hover { border-color: #58A6FF; background: #1C2333; }
  .template-card.selected { border-color: #7C3AED; background: #1C1528; }
  .template-radio {
    width: 14px; height: 14px; border-radius: 50%; border: 2px solid #484F58;
    flex-shrink: 0; position: relative; transition: border-color 0.2s;
  }
  .template-card.selected .template-radio { border-color: #7C3AED; }
  .template-card.selected .template-radio::after {
    content: ''; position: absolute; top: 2px; left: 2px;
    width: 6px; height: 6px; border-radius: 50%; background: #7C3AED;
  }
  .template-info { flex: 1; min-width: 0; }
  .template-name { font-size: 12px; color: #E6EDF3; font-weight: 500; }
  .template-desc { font-size: 10px; color: #8B949E; margin-top: 2px; }
  .prompt-section { margin-bottom: 12px; display: none; }
  .prompt-section.visible { display: block; }
  .prompt-section > label { display: block; font-size: 11px; color: #8B949E; font-weight: 500; margin-bottom: 4px; }
  .prompt-textarea {
    width: 100%; min-height: 100px; padding: 8px; background: #161B22;
    border: 1px solid #30363D; border-radius: 6px; color: #E6EDF3;
    font-size: 12px; font-family: -apple-system, monospace; line-height: 1.5;
    resize: vertical; outline: none;
  }
  .prompt-textarea:focus { border-color: #7C3AED; }
  .config-actions { display: flex; gap: 8px; align-items: center; }
  .config-saved { color: #3FB950; font-size: 12px; opacity: 0; transition: opacity 0.3s; }
  .config-saved.show { opacity: 1; }

  /* ===== 话题摘要卡片 ===== */
  .topic-stat-card {
    background: #161B22; border: 1px solid #21262D; border-radius: 8px;
    padding: 12px 14px; margin-bottom: 8px;
  }
  .topic-stat-card h3 {
    font-size: 13px; color: #58A6FF; margin-bottom: 6px;
    display: flex; align-items: center; gap: 8px;
  }
  .topic-stat-card .topic-meta { font-size: 11px; color: #8B949E; margin-bottom: 6px; }
  .topic-stat-card .topic-kw { display: flex; flex-wrap: wrap; gap: 4px; }
  .topic-stat-card .topic-kw span {
    background: #1C2333; padding: 2px 8px; border-radius: 10px;
    font-size: 11px; color: #A78BFA; border: 1px solid #30363D;
  }

  /* ===== 加载状态 ===== */
  .loading { text-align: center; padding: 40px; color: #8B949E; }
  .spinner {
    width: 28px; height: 28px; border: 3px solid #21262D; border-top-color: #58A6FF;
    border-radius: 50%; animation: spin 0.8s linear infinite; margin: 0 auto 12px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* ===== 滚动条美化 ===== */
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: #21262D; border-radius: 3px; }
  ::-webkit-scrollbar-thumb:hover { background: #30363D; }
</style>
</head>
<body>

<!-- ===== 顶部导航栏 ===== -->
<div class="header">
  <div class="logo">
    <div class="logo-bar"></div>
    <h1>Telegram Summary</h1>
    <span class="subtitle">一键生成群聊摘要</span>
  </div>
  <div class="nav-tabs">
    <button class="nav-tab active" onclick="switchView('messages')">消息浏览</button>
    <button class="nav-tab" onclick="switchView('summary')">统计摘要</button>
  </div>
  <div class="header-actions">
    <button class="btn-outline" onclick="refreshDb()" id="refreshBtn">刷新数据</button>
    <span id="aiStatus" style="font-size:11px;color:#484F58;"></span>
  </div>
</div>

<!-- ===== 三栏主布局 ===== -->
<div class="layout">
  <!-- 左栏：群组列表 -->
  <div class="left-panel">
    <div class="search-box">
      <input type="text" id="searchInput" placeholder="搜索群组..." oninput="filterChats()">
    </div>
    <div class="chat-list" id="chatList">
      <div class="loading"><div class="spinner"></div>加载群组列表...</div>
    </div>
    <div class="panel-footer">
      <button class="btn-outline" style="width:100%;text-align:center;" onclick="refreshDb()">+ 刷新群组</button>
    </div>
  </div>

  <!-- 中栏：消息 & 摘要 -->
  <div class="center-panel">
    <!-- 中栏头部：群名 + 控制 -->
    <div class="center-header" id="centerHeader" style="display:none;">
      <div class="center-title-row">
        <div>
          <div class="center-title" id="centerTitle"></div>
          <div class="center-date" id="centerDate"></div>
        </div>
        <div class="center-stats" id="centerStats">
          <span id="msgCountText"></span>
          <select id="msgLimitSelect" onchange="onMsgLimitChange()" style="
            margin-left:6px; padding:2px 6px; background:#161B22; border:1px solid #30363D;
            border-radius:4px; color:#8B949E; font-size:11px; cursor:pointer;">
            <option value="200">显示 200 条</option>
            <option value="500" selected>显示 500 条</option>
            <option value="1000">显示 1000 条</option>
            <option value="2000">显示 2000 条</option>
            <option value="0">显示全部</option>
          </select>
        </div>
      </div>
      <div class="center-controls">
        <input type="date" id="startDate" onchange="onDateChange()">
        <span style="color:#484F58;font-size:12px;">~</span>
        <input type="date" id="endDate" onchange="onDateChange()">
        <select id="daysPreset" onchange="onPresetChange(this.value)">
          <option value="1">1 天</option>
          <option value="3">3 天</option>
          <option value="7" selected>7 天</option>
          <option value="14">14 天</option>
          <option value="30">30 天</option>
          <option value="90">90 天</option>
        </select>
        <input type="hidden" id="daysInput" value="7">
        <button class="btn" id="genBtn" onclick="generateSummary()" disabled style="font-size:12px;padding:6px 12px;">生成摘要</button>
        <button class="btn btn-purple" id="aiBtn" onclick="generateAiSummary()" disabled style="font-size:12px;padding:6px 12px;">AI 智能分析</button>
      </div>
      <!-- 话题标签（动态渲染） -->
      <div class="topic-bar" id="topicBar" style="display:none;"></div>
    </div>
    <!-- 中栏内容 -->
    <div class="center-content" id="centerPanel">
      <div class="empty">选择左侧群组查看聊天记录</div>
    </div>
  </div>

  <!-- 右栏：AI 分析 + 配置 -->
  <div class="right-panel" id="rightPanel">
    <div class="right-header">
      <h2>&#10024; AI 智能分析</h2>
      <div class="ai-sub" id="aiSub">点击「AI 智能分析」开始</div>
    </div>
    <div class="right-content" id="aiResultSection">
      <div class="empty-hint">选择群组后点击<br>「AI 智能分析」按钮<br>结果将显示在这里</div>
    </div>
    <div class="right-footer">
      <button class="config-toggle" id="configToggle" onclick="toggleConfig()">&#9881; AI 配置</button>
      <button class="btn-outline" id="exportBtn" onclick="exportReport()" style="display:none;">&#128196; 导出报告</button>
    </div>
    <!-- AI 配置面板（在底栏上方展开） -->
    <div class="config-panel" id="configPanel" style="padding:0 16px;">
      <div class="config-inner">
        <div class="config-row">
          <div class="config-field">
            <label>AI 引擎</label>
            <select id="cfgMethod">
              <option value="auto">自动选择</option>
              <option value="claude_cli">Claude CLI</option>
              <option value="codex_cli">Codex CLI</option>
            </select>
          </div>
          <div class="config-field">
            <label>模型</label>
            <select id="cfgModel">
              <option value="">默认（Sonnet）</option>
              <option value="claude-haiku-4-5-20251001">Haiku（快速）</option>
              <option value="claude-sonnet-4-6">Sonnet 4.6</option>
              <option value="claude-opus-4-6">Opus 4.6（最强）</option>
            </select>
          </div>
        </div>
        <div class="config-row">
          <div class="config-field">
            <label>采样上限（条）</label>
            <input type="number" id="cfgMaxSample" value="500" min="50" max="2000" step="50">
          </div>
          <div class="config-field">
            <label>超时时间（秒）</label>
            <input type="number" id="cfgTimeout" value="300" min="30" max="600" step="30">
          </div>
        </div>
        <div class="template-section">
          <label>分析模板</label>
          <div class="template-list" id="templateList"></div>
        </div>
        <div class="prompt-section" id="customPromptSection">
          <label>自定义 Prompt</label>
          <textarea class="prompt-textarea" id="cfgPrompt" placeholder="输入你的分析指令..."></textarea>
        </div>
        <div class="config-actions">
          <button class="btn btn-purple" onclick="saveConfig()" style="font-size:12px;padding:6px 14px;">保存</button>
          <button class="btn-outline" onclick="resetConfig()">重置</button>
          <span class="config-saved" id="configSaved">&#10003; 已保存</span>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
let allChats = [];
let selectedChatId = null;
let selectedChatPeerType = 2;
// 话题相关状态
let currentTopics = {};           // {topic_id: topic_name}
let selectedTopicId = null;       // null=全部, 数字=指定话题
let lastMessagesData = null;      // 缓存最近一次消息数据（用于前端 topic 过滤）
let currentView = 'messages';     // messages / summary

// 预定义头像颜色（设计稿风格）
const AVATAR_COLORS = ['#F59E0B','#E85D04','#1F6FEB','#7C3AED','#238636','#E91E8C','#2DD4BF','#F85149','#CD7F32','#60A5FA'];
function avatarColor(str) { var h=0; for(var i=0;i<str.length;i++) h=str.charCodeAt(i)+((h<<5)-h); return AVATAR_COLORS[Math.abs(h)%AVATAR_COLORS.length]; }
function avatarLetter(str) { return (str||'?').charAt(0).toUpperCase(); }

// 导航标签切换
function switchView(view) {
  currentView = view;
  document.querySelectorAll('.nav-tab').forEach(function(t,i) {
    t.classList.toggle('active', (i===0 && view==='messages') || (i===1 && view==='summary'));
  });
  if (selectedChatId) {
    if (view === 'summary') generateSummary();
    else loadMessages();
  }
}

// 加载群列表
async function loadChats() {
  var controller = new AbortController();
  var timeoutId = setTimeout(function() { controller.abort(); }, 45000);
  try {
    console.time('loadChats');
    var res = await fetch('/api/chats', { signal: controller.signal });
    if (!res.ok) { var text = await res.text(); throw new Error('HTTP ' + res.status + ': ' + text.slice(0,200)); }
    var data = await res.json();
    if (!Array.isArray(data)) throw new Error(data.detail || '接口返回格式错误');
    allChats = data;
    renderChats(allChats);
    console.timeEnd('loadChats');
  } catch (e) {
    console.error('loadChats 失败:', e);
    var msg = e.name === 'AbortError' ? '请求超时（首次启动约 20-60 秒）' : e.message;
    document.getElementById('chatList').innerHTML = '<div class="loading" style="color:#F85149;">加载失败: ' + esc(msg) + '</div>';
  } finally { clearTimeout(timeoutId); }
}

// 渲染群组列表（左栏侧边栏）
function renderChats(chats) {
  var el = document.getElementById('chatList');
  if (!chats.length) { el.innerHTML = '<div class="empty-hint">没有找到群组</div>'; return; }
  // 按消息数排序，前3个作为"收藏群组"
  var sorted = chats.slice().sort(function(a,b){ return b.message_count - a.message_count; });
  var favIds = new Set(sorted.slice(0,3).map(function(c){ return c.peer_id; }));
  var favs = chats.filter(function(c){ return favIds.has(c.peer_id); });
  var rest = chats.filter(function(c){ return !favIds.has(c.peer_id); });

  var html = '<div class="section-label">&#9734; 收藏群组</div>';
  html += favs.map(function(c) { return chatItemHtml(c); }).join('');
  if (rest.length) {
    html += '<div class="section-label">&#9776; 全部群组</div>';
    html += rest.map(function(c) { return chatItemHtml(c); }).join('');
  }
  el.innerHTML = html;
}

// 单个群组条目 HTML
function chatItemHtml(c) {
  var color = avatarColor(c.title);
  var letter = avatarLetter(c.title);
  var isActive = c.peer_id === selectedChatId;
  return '<div class="chat-item' + (isActive ? ' active' : '') + '" onclick="selectChat(' + c.peer_id + ',' + c.peer_type + ')">'
    + '<div class="chat-avatar" style="background:' + color + ';">' + esc(letter) + '</div>'
    + '<div class="chat-info">'
    + '<div class="chat-title">' + esc(c.title) + '</div>'
    + '<div class="chat-meta">' + c.message_count.toLocaleString() + ' 条'
    + (c.username ? ' · @' + esc(c.username) : '') + '</div>'
    + '</div>'
    + '<div class="chat-status">' + (isActive ? '已选' : '') + '</div>'
    + '</div>';
}

// 搜索过滤
function filterChats() {
  var q = document.getElementById('searchInput').value.toLowerCase();
  var filtered = allChats.filter(function(c) {
    return c.title.toLowerCase().includes(q) || (c.username||'').toLowerCase().includes(q);
  });
  renderChats(filtered);
}

// 选中群并自动加载聊天记录
function selectChat(peerId, peerType) {
  selectedChatId = peerId;
  selectedChatPeerType = peerType || 2;
  document.getElementById('genBtn').disabled = false;
  if (aiAvailable) document.getElementById('aiBtn').disabled = false;
  // 重新渲染群列表（高亮选中项）
  renderChats(allChats.filter(function(c) {
    var q = document.getElementById('searchInput').value.toLowerCase();
    return !q || c.title.toLowerCase().includes(q) || (c.username||'').toLowerCase().includes(q);
  }));
  // 显示中栏头部
  var chat = allChats.find(function(c){ return c.peer_id === peerId; });
  document.getElementById('centerHeader').style.display = '';
  document.getElementById('centerTitle').textContent = chat ? chat.title : '';
  // 加载消息
  loadMessages();
}

// 加载并展示聊天记录
async function loadMessages() {
  if (!selectedChatId) return;
  var centerPanel = document.getElementById('centerPanel');
  centerPanel.innerHTML = '<div class="loading"><div class="spinner"></div>加载聊天记录...</div>';

  var days = document.getElementById('daysInput').value;
  var start = document.getElementById('startDate').value;
  var end = document.getElementById('endDate').value;

  // 从右上角下拉框读取展示数量（0 表示不限）
  var msgLimit = parseInt(document.getElementById('msgLimitSelect').value) || 0;
  var url = '/api/messages?chat_id=' + selectedChatId + '&peer_type=' + selectedChatPeerType + '&days=' + days + (msgLimit ? '&limit=' + msgLimit : '&limit=99999');
  if (start) url += '&start=' + start;
  if (end) url += '&end=' + end;

  try {
    var res = await fetch(url);
    var data = await res.json();
    currentTopics = data.topics || {};
    lastMessagesData = data;
    selectedTopicId = null;

    if (data.error && !data.messages.length) {
      centerPanel.innerHTML = '<div class="empty">' + esc(data.error) + '</div>';
    } else {
      renderMessages(data);
    }
  } catch (e) {
    centerPanel.innerHTML = '<div class="empty">加载失败: ' + esc(e.message) + '</div>';
  }
}

// 切换话题过滤
function selectTopic(topicId) {
  selectedTopicId = topicId;
  if (lastMessagesData) renderMessages(lastMessagesData);
}

// 渲染聊天记录列表
function renderMessages(d) {
  var allMsgs = d.messages || [];
  var topics = d.topics || {};
  var msgs = allMsgs;
  if (selectedTopicId !== null) {
    msgs = allMsgs.filter(function(m) { return m.topic_id !== null && String(m.topic_id) === String(selectedTopicId); });
  }

  // 更新中栏头部信息
  document.getElementById('centerDate').textContent = d.start + ' ~ ' + d.end;
  // 显示总消息数（区分总数和当前展示数）
  var totalCount = d.total_count || msgs.length;
  var shownCount = msgs.length;
  var countText = '共 ' + totalCount.toLocaleString() + ' 条消息';
  if (shownCount < totalCount) countText += '（展示 ' + shownCount.toLocaleString() + ' 条）';
  document.getElementById('msgCountText').textContent = countText;

  // 渲染话题标签栏
  renderTopicBar(allMsgs, topics);

  // 渲染消息列表（带彩色头像）
  var centerPanel = document.getElementById('centerPanel');
  // 按话题分组
  var grouped = groupMessagesByTopic(msgs, topics);
  var html = '';
  grouped.forEach(function(group) {
    if (group.label) {
      html += '<div class="msg-section-header">'
        + '<span class="section-icon">#</span> ' + esc(group.label)
        + '<span style="margin-left:auto;font-size:11px;color:#484F58;">' + group.msgs.length + ' 条消息</span>'
        + '</div>';
    }
    html += '<div class="msg-list">';
    html += group.msgs.map(function(m) {
      var color = avatarColor(m.author);
      var letter = avatarLetter(m.author);
      return '<div class="msg-item">'
        + '<div class="msg-avatar" style="background:' + color + ';">' + esc(letter) + '</div>'
        + '<div class="msg-body">'
        + '<span class="msg-author" style="color:' + color + ';">' + esc(m.author) + '</span>'
        + (m.reply_to ? '<span class="msg-reply-tag">回复</span>' : '')
        + '<div class="msg-text">' + esc(m.text) + '</div>'
        + '</div>'
        + '<span class="msg-time">' + esc(m.date) + '</span>'
        + '</div>';
    }).join('');
    html += '</div>';
  });
  centerPanel.innerHTML = html || '<div class="empty">暂无消息</div>';
}

// 按话题分组消息
function groupMessagesByTopic(msgs, topics) {
  if (selectedTopicId !== null || !topics || Object.keys(topics).length < 2) {
    return [{ label: '', msgs: msgs.slice(-200) }];
  }
  // 有多话题时，按 topic_id 分组展示
  var groups = {};
  var order = [];
  msgs.slice(-200).forEach(function(m) {
    var key = m.topic_id != null ? String(m.topic_id) : '_none';
    if (!groups[key]) { groups[key] = []; order.push(key); }
    groups[key].push(m);
  });
  return order.map(function(key) {
    var label = key === '_none' ? '' : (topics[key] || 'Topic #' + key);
    return { label: label, msgs: groups[key] };
  });
}

// 渲染话题标签栏（中栏头部内）
function renderTopicBar(msgs, topics) {
  var bar = document.getElementById('topicBar');
  if (!topics || Object.keys(topics).length < 2) { bar.style.display = 'none'; return; }
  bar.style.display = 'flex';

  var topicCounts = {};
  msgs.forEach(function(m) { if (m.topic_id != null) topicCounts[m.topic_id] = (topicCounts[m.topic_id]||0)+1; });
  var sorted = Object.keys(topicCounts).sort(function(a,b){ return (topicCounts[b]||0)-(topicCounts[a]||0); });

  var html = '<div class="topic-tab' + (selectedTopicId===null?' active':'') + '" onclick="selectTopic(null)">全部消息<span class="topic-count">' + msgs.length + '</span></div>';
  sorted.forEach(function(tid) {
    var name = topics[tid] || 'Topic #' + tid;
    if (name.length > 10) name = name.slice(0,9) + '…';
    html += '<div class="topic-tab' + (String(selectedTopicId)===String(tid)?' active':'') + '" onclick="selectTopic(' + tid + ')">'
      + esc(name) + '<span class="topic-count">' + (topicCounts[tid]||0) + '</span></div>';
  });
  bar.innerHTML = html;
}

// 生成摘要
async function generateSummary() {
  if (!selectedChatId) return;
  var btn = document.getElementById('genBtn');
  var centerPanel = document.getElementById('centerPanel');
  btn.disabled = true; btn.textContent = '生成中...';
  centerPanel.innerHTML = '<div class="loading"><div class="spinner"></div>正在生成摘要...</div>';

  var days = document.getElementById('daysInput').value;
  var start = document.getElementById('startDate').value;
  var end = document.getElementById('endDate').value;

  var url = '/api/summary?chat_id=' + selectedChatId + '&peer_type=' + selectedChatPeerType + '&days=' + days;
  if (start) url += '&start=' + start;
  if (end) url += '&end=' + end;
  if (selectedTopicId !== null) url += '&topic_id=' + selectedTopicId;

  try {
    var res = await fetch(url);
    var data = await res.json();
    if (data.error) {
      centerPanel.innerHTML = '<div class="empty">' + esc(data.error) + '</div>';
    } else {
      if (data.topics) currentTopics = data.topics;
      renderReport(data);
    }
  } catch (e) {
    centerPanel.innerHTML = '<div class="empty">请求失败: ' + esc(e.message) + '</div>';
  }
  btn.disabled = false; btn.textContent = '生成摘要';
}

// 渲染报告
function renderReport(d) {
  var maxUser = d.top_users.length ? d.top_users[0].count : 1;
  var maxHour = Math.max.apply(null, d.hourly_activity.map(function(h){return h.count;}).concat([1]));
  var centerPanel = document.getElementById('centerPanel');
  var topicStats = d.topic_stats || [];
  var topics = d.topics || {};

  // 话题分组统计
  var topicSection = '';
  if (topicStats.length > 0 && selectedTopicId === null) {
    topicSection = '<div class="card"><h2>话题分组统计</h2>';
    topicStats.forEach(function(ts) {
      var kwHtml = ts.top_keywords.map(function(k){ return '<span>' + esc(k.word) + '</span>'; }).join('');
      var usersHtml = ts.top_users.map(function(u){ return esc(u.name) + '(' + u.count + ')'; }).join(', ');
      topicSection += '<div class="topic-stat-card">'
        + '<h3><span style="cursor:pointer;" onclick="selectTopic(' + ts.topic_id + ')">' + esc(ts.topic_name) + '</span>'
        + '<span style="font-size:11px;color:#8B949E;font-weight:normal;margin-left:8px;">' + ts.message_count + ' 条 · ' + ts.active_users + ' 人</span></h3>'
        + '<div class="topic-meta">活跃: ' + usersHtml + '</div>'
        + (kwHtml ? '<div class="topic-kw">' + kwHtml + '</div>' : '')
        + '</div>';
    });
    topicSection += '</div>';
  }

  centerPanel.innerHTML =
    '<div class="stats">'
    + '<div class="stat-box"><div class="number">' + d.message_count.toLocaleString() + '</div><div class="label">消息总数</div></div>'
    + '<div class="stat-box"><div class="number">' + d.active_user_count + '</div><div class="label">活跃用户</div></div>'
    + '<div class="stat-box"><div class="number">' + d.top_keywords.length + '</div><div class="label">话题关键词</div></div>'
    + '<div class="stat-box"><div class="number">' + (topicStats.length || '-') + '</div><div class="label">Forum 话题</div></div>'
    + '</div>'
    + topicSection
    + '<div class="card"><h2>活跃用户排行</h2>'
    + d.top_users.map(function(u) {
        return '<div class="user-bar">'
          + '<div class="name" title="' + esc(u.name) + '">' + esc(u.name) + '</div>'
          + '<div class="bar-bg"><div class="bar-fill" style="width:' + (u.count/maxUser*100).toFixed(1) + '%"></div></div>'
          + '<div class="count">' + u.count + '</div></div>';
      }).join('')
    + '</div>'
    + '<div class="card"><h2>话题关键词</h2><div class="keywords">'
    + d.top_keywords.map(function(k) {
        return '<span class="keyword"><span class="w">' + esc(k.word) + '</span><span class="c">' + k.count + '</span></span>';
      }).join('')
    + '</div></div>'
    + '<div class="card"><h2>按小时活跃度</h2><div class="hour-chart">'
    + d.hourly_activity.map(function(h) {
        return '<div class="hour-bar-wrap"><div class="hour-bar" style="height:' + (h.count/maxHour*100).toFixed(1) + '%" title="' + h.hour + ': ' + h.count + ' 条"></div>'
          + '<div class="hour-label">' + h.hour.replace(':00','') + '</div></div>';
      }).join('')
    + '</div></div>'
    + (d.action_items.length ? '<div class="card"><h2>潜在行动项</h2>' + d.action_items.map(function(a){ return '<div class="action-item">' + esc(a) + '</div>'; }).join('') + '</div>' : '')
    + (d.hot_replies.length ? '<div class="card"><h2>热门讨论</h2>' + d.hot_replies.map(function(r){ return '<div class="action-item">' + esc(r) + '</div>'; }).join('') + '</div>' : '');

  // 更新中栏头部
  document.getElementById('centerDate').textContent = d.start + ' ~ ' + d.end;
  document.getElementById('msgCountText').textContent = '共 ' + d.message_count.toLocaleString() + ' 条消息';
}

// ─── AI 功能 ───

let aiAvailable = false;

// 检查 AI 是否可用
async function checkAiStatus() {
  try {
    const res = await fetch('/api/ai-status');
    const data = await res.json();
    aiAvailable = data.available;
    var el = document.getElementById('aiStatus');
    if (data.available) {
      el.textContent = data.detail;
      el.style.color = '#A78BFA';
    } else {
      el.textContent = data.detail;
      document.getElementById('aiBtn').style.display = 'none';
    }
  } catch (e) {
    document.getElementById('aiBtn').style.display = 'none';
  }
}

// Markdown → HTML 转换
function mdToHtml(md) {
  return md
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm, '<h3 style="font-size:14px;color:#A78BFA;">$1</h3>')
    .replace(/^# (.+)$/gm, '<h3 style="font-size:16px;color:#A78BFA;">$1</h3>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/^\- (.+)$/gm, '<li>$1</li>')
    .replace(/^\d+\.\s+(.+)$/gm, '<li>$1</li>')
    .replace(/`([^`]+)`/g, '<code style="background:#1C2333;padding:2px 6px;border-radius:4px;font-size:12px;">$1</code>')
    .replace(/\\n\\n/g, '<br><br>')
    .replace(/\\n/g, '<br>');
}

// 生成 AI 摘要（输出到右栏）
async function generateAiSummary() {
  if (!selectedChatId || !aiAvailable) return;
  var btn = document.getElementById('aiBtn');
  var aiSection = document.getElementById('aiResultSection');

  btn.disabled = true; btn.textContent = 'AI 分析中...';
  document.getElementById('aiSub').textContent = '正在分析...';
  aiSection.innerHTML = '<div class="loading"><div class="spinner"></div>Claude AI 深度分析中（约 30-60 秒）...</div>';

  var days = document.getElementById('daysInput').value;
  var start = document.getElementById('startDate').value;
  var end = document.getElementById('endDate').value;

  var url = '/api/ai-summary?chat_id=' + selectedChatId + '&peer_type=' + selectedChatPeerType + '&days=' + days;
  if (start) url += '&start=' + start;
  if (end) url += '&end=' + end;
  // 如果选中了话题 tab，只分析该话题的消息
  if (selectedTopicId !== null) url += '&topic_id=' + selectedTopicId;

  try {
    var res = await fetch(url);
    var data = await res.json();
    if (data.error) {
      aiSection.innerHTML = '<div class="ai-card"><h2>AI 分析失败</h2><p style="color:#F85149;">' + esc(data.error) + '</p></div>';
      document.getElementById('aiSub').textContent = '分析失败';
    } else {
      var methodLabel = data.method === 'claude_cli' ? 'Claude CLI' : 'Codex CLI';
      // 副标题：显示话题名称（如果有）+ 消息条数
      var subText = '基于 ' + data.message_count + ' 条消息的深度分析';
      if (data.topic_name) subText = '话题「' + data.topic_name + '」· ' + subText;
      document.getElementById('aiSub').textContent = subText;
      document.getElementById('exportBtn').style.display = '';
      // 标题区域：显示话题标签（如果有）
      var topicBadge = data.topic_name ? '<span class="ai-badge" style="background:#7C3AED;">' + esc(data.topic_name) + '</span>' : '';
      aiSection.innerHTML =
        '<div class="ai-card">'
        + '<h2>AI 智能分析 <span class="ai-badge">' + methodLabel + '</span>'
        + '<span class="ai-badge">' + data.message_count + ' 条</span>'
        + topicBadge + '</h2>'
        + '<div class="ai-content">' + mdToHtml(data.ai_summary) + '</div>'
        + '</div>';
    }
  } catch (e) {
    aiSection.innerHTML = '<div class="ai-card"><h2>AI 分析失败</h2><p style="color:#F85149;">请求错误: ' + esc(e.message) + '</p></div>';
    document.getElementById('aiSub').textContent = '分析失败';
  }
  btn.disabled = false; btn.textContent = 'AI 智能分析';
}

// 导出报告（简单复制文本）
function exportReport() {
  var content = document.getElementById('aiResultSection').innerText;
  if (navigator.clipboard) {
    navigator.clipboard.writeText(content).then(function() { alert('报告已复制到剪贴板'); });
  }
}

// 刷新数据库缓存
async function refreshDb() {
  var btn = document.getElementById('refreshBtn');
  btn.disabled = true; btn.textContent = '刷新中...';
  try {
    var res = await fetch('/api/refresh', {method:'POST'});
    var data = await res.json();
    if (data.ok) {
      btn.textContent = '已刷新';
      loadChats();
    } else {
      btn.textContent = '刷新失败';
    }
  } catch(e) { btn.textContent = '刷新失败'; }
  setTimeout(function(){ btn.disabled = false; btn.textContent = '刷新数据'; }, 3000);
}

function esc(s) { var d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

// ─── AI 配置管理 ───

// 缓存模板列表（页面生命周期内只拉一次）
let _templates = [];
let _selectedTemplate = 'general';

function toggleConfig() {
  var panel = document.getElementById('configPanel');
  var btn = document.getElementById('configToggle');
  panel.classList.toggle('open');
  btn.classList.toggle('active');
  if (panel.classList.contains('open') && !panel.dataset.loaded) {
    loadConfig();
    panel.dataset.loaded = '1';
  }
}

// 渲染模板选择器卡片
function renderTemplates(selectedId) {
  var list = document.getElementById('templateList');
  list.innerHTML = _templates.map(function(t) {
    var sel = t.id === selectedId ? ' selected' : '';
    return '<div class="template-card' + sel + '" onclick="selectTemplate(\\'' + t.id + '\\')">'
      + '<div class="template-radio"></div>'
      + '<div class="template-info">'
      + '<div class="template-name">' + esc(t.name) + '</div>'
      + '<div class="template-desc">' + esc(t.description) + '</div>'
      + '</div></div>';
  }).join('');
  _selectedTemplate = selectedId;
  // custom 模板时显示 textarea，否则隐藏
  var ps = document.getElementById('customPromptSection');
  if (selectedId === 'custom') { ps.classList.add('visible'); }
  else { ps.classList.remove('visible'); }
}

// 用户点击模板卡片
function selectTemplate(id) {
  renderTemplates(id);
}

async function loadConfig() {
  try {
    // 并行拉取模板列表和当前配置
    var [tplRes, cfgRes] = await Promise.all([
      fetch('/api/ai-templates'),
      fetch('/api/ai-config')
    ]);
    _templates = await tplRes.json();
    var cfg = await cfgRes.json();
    // 渲染模板卡片
    renderTemplates(cfg.template || 'general');
    // 填充其他字段
    document.getElementById('cfgMethod').value = cfg.method;
    document.getElementById('cfgModel').value = cfg.cli_model || '';
    document.getElementById('cfgMaxSample').value = cfg.max_sample_messages;
    document.getElementById('cfgTimeout').value = cfg.cli_timeout;
    document.getElementById('cfgPrompt').value = cfg.system_prompt;
  } catch(e) { console.error('loadConfig error:', e); }
}

async function saveConfig() {
  var cfg = {
    method: document.getElementById('cfgMethod').value,
    cli_model: document.getElementById('cfgModel').value,
    max_sample_messages: parseInt(document.getElementById('cfgMaxSample').value),
    cli_timeout: parseInt(document.getElementById('cfgTimeout').value),
    template: _selectedTemplate
  };
  if (_selectedTemplate === 'custom') {
    cfg.system_prompt = document.getElementById('cfgPrompt').value;
  }
  try {
    var res = await fetch('/api/ai-config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(cfg)
    });
    var data = await res.json();
    if (data.ok) showConfigSaved();
  } catch(e) { alert('保存失败: ' + e.message); }
}

async function resetConfig() {
  if (!confirm('确定恢复为默认配置？')) return;
  try {
    var res = await fetch('/api/ai-config/reset', {method: 'POST'});
    var data = await res.json();
    if (data.ok) { loadConfig(); showConfigSaved(); }
  } catch(e) { alert('重置失败: ' + e.message); }
}

function showConfigSaved() {
  var el = document.getElementById('configSaved');
  el.classList.add('show');
  setTimeout(function(){ el.classList.remove('show'); }, 2000);
}

// ─── 日期管理 ───

function fmtDate(d) {
  return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0');
}

function initDates() {
  var end = new Date();
  var days = parseInt(document.getElementById('daysPreset').value) || 7;
  var start = new Date();
  start.setDate(end.getDate() - days);
  document.getElementById('endDate').value = fmtDate(end);
  document.getElementById('startDate').value = fmtDate(start);
  document.getElementById('daysInput').value = days;
}

function onPresetChange(val) {
  var days = parseInt(val) || 7;
  var end = new Date();
  var start = new Date();
  start.setDate(end.getDate() - days);
  document.getElementById('endDate').value = fmtDate(end);
  document.getElementById('startDate').value = fmtDate(start);
  document.getElementById('daysInput').value = days;
  if (selectedChatId) loadMessages();
}

function onDateChange() {
  var s = document.getElementById('startDate').value;
  var e = document.getElementById('endDate').value;
  if (s && e) {
    var diff = Math.round((new Date(e) - new Date(s)) / 86400000);
    document.getElementById('daysInput').value = Math.max(1, diff);
  }
  if (selectedChatId) loadMessages();
}

// 消息展示数量变化时重新加载
function onMsgLimitChange() {
  if (selectedChatId) loadMessages();
}

initDates();
loadChats();
checkAiStatus();
</script>
</body>
</html>
"""


# ──────────────────────────────────────────────
# 启动入口
# ──────────────────────────────────────────────

def main():
    import uvicorn
    print("\\n  Telegram Summary Tool - Web Dashboard")
    print("  打开浏览器访问: http://127.0.0.1:8877\\n")
    uvicorn.run(app, host="127.0.0.1", port=8877, log_level="info")


if __name__ == "__main__":
    main()
