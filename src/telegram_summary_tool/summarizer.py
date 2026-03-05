from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import re

from .telegram_export import TelegramMessage


EN_STOPWORDS = {
    "the",
    "and",
    "for",
    "that",
    "this",
    "with",
    "from",
    "have",
    "will",
    "your",
    "you",
    "are",
    "not",
    "but",
    "was",
    "were",
    "can",
    "our",
    "its",
    "about",
    "into",
    "what",
    "when",
    "where",
    "how",
    "please",
    "just",
    "they",
    "them",
    "then",
    "there",
}

ZH_STOPWORDS = {
    "我们",
    "你们",
    "他们",
    "这个",
    "那个",
    "今天",
    "明天",
    "然后",
    "已经",
    "一下",
    "可以",
    "需要",
    "就是",
    "因为",
    "所以",
    "如果",
    "没有",
    "还是",
    "一个",
    "不是",
    "自己",
}

ACTION_PATTERN = re.compile(
    r"(待办|todo|to do|行动项|需要|请|截止|deadline|安排|跟进|负责|确认|修复|上线|发布|本周|下周)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TopicStat:
    """单个话题的统计信息"""
    topic_id: int
    topic_name: str
    message_count: int
    active_users: int
    top_users: list[tuple[str, int]]      # 该话题下的活跃用户
    top_keywords: list[tuple[str, int]]   # 该话题的热门关键词


@dataclass(frozen=True)
class SummaryReport:
    chat_name: str
    start: datetime
    end: datetime
    message_count: int
    active_user_count: int
    top_users: list[tuple[str, int]]
    top_keywords: list[tuple[str, int]]
    action_items: list[str]
    hot_replies: list[str]
    hourly_activity: list[tuple[str, int]]
    topic_stats: list[TopicStat] = ()  # 按话题分组的统计（空=非 Forum 群组）


def filter_messages_by_range(
    messages: list[TelegramMessage], start: datetime, end: datetime
) -> list[TelegramMessage]:
    return [m for m in messages if start <= m.date <= end]


def _cjk_tokens(token: str) -> list[str]:
    cleaned = token.strip()
    if len(cleaned) < 2:
        return []
    if len(cleaned) <= 8:
        return [cleaned]
    chunks = [cleaned[i : i + 4] for i in range(0, len(cleaned), 4)]
    return [chunk for chunk in chunks if len(chunk) >= 2]


def extract_keywords(text: str) -> list[str]:
    tokens: list[str] = []

    for raw in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", text):
        word = raw.lower()
        if word in EN_STOPWORDS:
            continue
        tokens.append(word)

    for raw in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        for word in _cjk_tokens(raw):
            if word in ZH_STOPWORDS:
                continue
            tokens.append(word)

    return tokens


def _truncate(text: str, size: int = 120) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= size:
        return compact
    return compact[: size - 3] + "..."


def build_summary_report(
    chat_name: str,
    messages: list[TelegramMessage],
    start: datetime,
    end: datetime,
    top_users: int,
    top_keywords: int,
    max_actions: int,
    topics: dict[int, str] | None = None,
) -> SummaryReport:
    users = Counter(m.author for m in messages)
    keyword_counter: Counter[str] = Counter()
    action_items: list[str] = []
    hour_counter: Counter[str] = Counter()
    msg_by_id = {m.message_id: m for m in messages}
    reply_counter: Counter[int] = Counter()

    seen_actions: set[str] = set()

    for m in messages:
        keyword_counter.update(extract_keywords(m.text))
        hour_counter[m.date.strftime("%H:00")] += 1

        if m.reply_to_message_id is not None:
            reply_counter[m.reply_to_message_id] += 1

        if ACTION_PATTERN.search(m.text):
            item = f"- [{m.date.strftime('%Y-%m-%d %H:%M')}] {m.author}: {_truncate(m.text)}"
            if item not in seen_actions:
                seen_actions.add(item)
                action_items.append(item)
            if len(action_items) >= max_actions:
                pass

    if len(action_items) > max_actions:
        action_items = action_items[:max_actions]

    hot_replies: list[str] = []
    for target_id, count in reply_counter.most_common(5):
        target = msg_by_id.get(target_id)
        if target is None:
            continue
        hot_replies.append(
            f"- {count} replies -> [{target.date.strftime('%m-%d %H:%M')}] "
            f"{target.author}: {_truncate(target.text, 90)}"
        )

    # 按话题分组统计（仅 Forum 群组有 topics）
    topic_stats_list: list[TopicStat] = []
    if topics:
        # 将消息按 topic_id 分桶
        from collections import defaultdict
        topic_buckets: dict[int, list[TelegramMessage]] = defaultdict(list)
        for m in messages:
            if m.topic_id is not None:
                topic_buckets[m.topic_id].append(m)

        # 按消息数降序排列
        for tid in sorted(topic_buckets, key=lambda t: len(topic_buckets[t]), reverse=True):
            bucket = topic_buckets[tid]
            t_users = Counter(m.author for m in bucket)
            t_kw: Counter[str] = Counter()
            for m in bucket:
                t_kw.update(extract_keywords(m.text))

            topic_stats_list.append(TopicStat(
                topic_id=tid,
                topic_name=topics.get(tid, f"Topic #{tid}"),
                message_count=len(bucket),
                active_users=len(t_users),
                top_users=t_users.most_common(5),
                top_keywords=t_kw.most_common(10),
            ))

    return SummaryReport(
        chat_name=chat_name,
        start=start,
        end=end,
        message_count=len(messages),
        active_user_count=len(users),
        top_users=users.most_common(top_users),
        top_keywords=keyword_counter.most_common(top_keywords),
        action_items=action_items[:max_actions],
        hot_replies=hot_replies,
        hourly_activity=sorted(hour_counter.items(), key=lambda x: x[0]),
        topic_stats=topic_stats_list,
    )


def render_markdown(report: SummaryReport) -> str:
    lines: list[str] = []
    lines.append(f"# Telegram Group Summary: {report.chat_name}")
    lines.append("")
    lines.append(
        f"Period: `{report.start.strftime('%Y-%m-%d %H:%M')}` -> `{report.end.strftime('%Y-%m-%d %H:%M')}`"
    )
    lines.append("")
    lines.append("## Snapshot")
    lines.append(f"- Total messages: **{report.message_count}**")
    lines.append(f"- Active participants: **{report.active_user_count}**")
    lines.append("")

    lines.append("## Top Participants")
    if report.top_users:
        for name, count in report.top_users:
            lines.append(f"- {name}: {count}")
    else:
        lines.append("- None")
    lines.append("")

    lines.append("## Topic Keywords")
    if report.top_keywords:
        joined = ", ".join([f"`{k}` ({v})" for k, v in report.top_keywords])
        lines.append(joined)
    else:
        lines.append("- None")
    lines.append("")

    lines.append("## Potential Action Items")
    if report.action_items:
        lines.extend(report.action_items)
    else:
        lines.append("- No explicit action-like messages found.")
    lines.append("")

    lines.append("## Most Discussed Messages")
    if report.hot_replies:
        lines.extend(report.hot_replies)
    else:
        lines.append("- No reply clusters found in this period.")
    lines.append("")

    lines.append("## Activity By Hour")
    if report.hourly_activity:
        for hour, count in report.hourly_activity:
            lines.append(f"- {hour}: {count}")
    else:
        lines.append("- None")
    lines.append("")

    # 话题分组统计（仅 Forum 群组显示）
    if report.topic_stats:
        lines.append("## Forum Topics Breakdown")
        for ts in report.topic_stats:
            lines.append(f"### {ts.topic_name}")
            lines.append(f"- Messages: {ts.message_count}, Active users: {ts.active_users}")
            if ts.top_users:
                user_str = ", ".join(f"{n}({c})" for n, c in ts.top_users[:3])
                lines.append(f"- Top users: {user_str}")
            if ts.top_keywords:
                kw_str = ", ".join(f"`{w}`" for w, _ in ts.top_keywords[:5])
                lines.append(f"- Keywords: {kw_str}")
            lines.append("")

    lines.append("## Suggested Follow-ups")
    lines.append("- Confirm owners and deadlines for each action item.")
    lines.append("- Pin the top 3 decisions from the discussed messages.")
    lines.append("- Compare with the previous period to detect new blockers.")
    lines.append("")

    return "\n".join(lines)
