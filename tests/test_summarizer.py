from pathlib import Path

from telegram_summary_tool.summarizer import build_summary_report, render_markdown
from telegram_summary_tool.telegram_export import load_telegram_export


SAMPLE_EXPORT = Path(__file__).resolve().parents[1] / "examples" / "sample-result.json"


def test_build_summary_report_and_render():
    export = load_telegram_export(SAMPLE_EXPORT)
    messages = export.messages

    report = build_summary_report(
        chat_name=export.chat_name,
        messages=messages,
        start=messages[0].date,
        end=messages[-1].date,
        top_users=3,
        top_keywords=10,
        max_actions=5,
    )
    markdown = render_markdown(report)

    assert report.message_count == 5
    assert report.active_user_count == 3
    assert "README" in markdown
    assert "Top Participants" in markdown
