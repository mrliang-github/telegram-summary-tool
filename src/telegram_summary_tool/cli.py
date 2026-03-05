from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path
import sys

from .summarizer import (
    build_summary_report,
    filter_messages_by_range,
    render_markdown,
)
from .telegram_export import load_telegram_export
from .tgmix_integration import (
    load_messages_from_tgmix_toon,
    resolve_export_dir,
    run_tgmix,
)


def _parse_date(value: str, end_of_day: bool) -> datetime:
    dt = datetime.strptime(value, "%Y-%m-%d")
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59)
    return dt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tg-summary",
        description=(
            "Generate Markdown summaries from Telegram data. "
            "Supports: tgmix (default), raw-export, or local (macOS Telegram app)."
        ),
    )
    parser.add_argument(
        "input",
        nargs="?",  # local 模式不需要 input 参数
        default=None,
        help=(
            "Path to Telegram export directory or result.json file. "
            "Not required for --source local."
        ),
    )
    parser.add_argument(
        "--source",
        choices=["tgmix", "raw-export", "local"],
        default="tgmix",
        help=(
            "Data source mode. "
            "'tgmix': open-source tgmix preprocessing. "
            "'raw-export': direct result.json parsing. "
            "'local': read from macOS Telegram app's local database. "
            "Default: tgmix"
        ),
    )
    parser.add_argument(
        "--list-chats",
        action="store_true",
        help="List available chats from local Telegram database, then exit.",
    )
    parser.add_argument(
        "--chat-id",
        type=int,
        help="Peer ID of the chat to summarize (for --source local).",
    )
    parser.add_argument(
        "--anonymize",
        action="store_true",
        help="Enable tgmix anonymization when --source=tgmix.",
    )
    parser.add_argument(
        "--reuse-preprocessed",
        action="store_true",
        help=(
            "Reuse existing tgmix_output.toon.txt if present, "
            "instead of rerunning tgmix."
        ),
    )
    parser.add_argument(
        "--start",
        help="Start date in YYYY-MM-DD.",
    )
    parser.add_argument(
        "--end",
        help="End date in YYYY-MM-DD (inclusive).",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=1,
        help="Lookback days if --start is not provided. Default: 1",
    )
    parser.add_argument(
        "--top-users",
        type=int,
        default=10,
        help="How many users to include. Default: 10",
    )
    parser.add_argument(
        "--top-keywords",
        type=int,
        default=20,
        help="How many keywords to include. Default: 20",
    )
    parser.add_argument(
        "--max-actions",
        type=int,
        default=12,
        help="Max potential action items. Default: 12",
    )
    parser.add_argument(
        "--output",
        help="Optional path for Markdown output.",
    )
    return parser


def _handle_list_chats() -> None:
    """列出本地 Telegram 数据库中的群组/频道"""
    from .local_db import list_chats

    chats = list_chats(min_messages=10)
    if not chats:
        print("No chats found in local Telegram database.")
        return

    print(f"\nFound {len(chats)} chats:\n")
    print(f"{'ID':>12}  {'Messages':>8}  {'Title'}")
    print("-" * 60)
    for c in chats:
        un = f" (@{c.username})" if c.username else ""
        print(f"{c.peer_id:>12}  {c.message_count:>8}  {c.title}{un}")
    print()
    print("Usage: tg-summary --source local --chat-id <ID> --days 7")


def _handle_local_source(args: argparse.Namespace):
    """处理 --source local 模式"""
    from .local_db import list_chats, load_messages_from_local

    if not args.chat_id:
        # 没有指定 chat_id，显示列表
        _handle_list_chats()
        raise SystemExit(0)

    print(f"Reading local Telegram database for chat {args.chat_id}...")
    chat_name, messages, topics = load_messages_from_local(
        peer_id=args.chat_id,
        peer_type=2,  # 默认 Channel
    )
    # topics 暂时在 CLI 模式下不使用，仅返回 chat_name 和 messages
    return chat_name, messages


def main() -> None:
    args = build_parser().parse_args()

    # 处理 --list-chats 快捷命令
    if args.list_chats:
        _handle_list_chats()
        return

    # 根据 source 模式加载数据
    if args.source == "local":
        chat_name, messages = _handle_local_source(args)

    elif args.source == "tgmix":
        if not args.input:
            raise SystemExit("Error: input path is required for --source tgmix.")
        try:
            export_dir = resolve_export_dir(args.input)
            tgmix_result = run_tgmix(
                export_dir=export_dir,
                anonymize=args.anonymize,
                skip_if_exists=args.reuse_preprocessed,
            )
            chat_name, messages = load_messages_from_tgmix_toon(tgmix_result.toon_file)
        except Exception as exc:
            raise SystemExit(
                "tgmix pipeline failed. "
                "Try --source raw-export as fallback.\n"
                f"Details: {exc}"
            )

    else:  # raw-export
        if not args.input:
            raise SystemExit("Error: input path is required for --source raw-export.")
        export = load_telegram_export(args.input)
        chat_name = export.chat_name
        messages = export.messages

    if not messages:
        raise SystemExit("No parseable text messages found.")

    # 计算时间范围
    last_message_time = messages[-1].date
    end = _parse_date(args.end, end_of_day=True) if args.end else last_message_time

    if args.start:
        start = _parse_date(args.start, end_of_day=False)
    else:
        start = end - timedelta(days=max(1, args.days))

    if start > end:
        raise SystemExit("--start cannot be later than --end.")

    selected = filter_messages_by_range(messages, start, end)
    if not selected:
        raise SystemExit("No messages in selected date range.")

    # 生成报告
    report = build_summary_report(
        chat_name=chat_name,
        messages=selected,
        start=start,
        end=end,
        top_users=max(1, args.top_users),
        top_keywords=max(1, args.top_keywords),
        max_actions=max(1, args.max_actions),
    )
    markdown = render_markdown(report)

    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown, encoding="utf-8")
        print(f"Summary saved to: {output_path}")
        return

    sys.stdout.write(markdown + "\n")


if __name__ == "__main__":
    main()
