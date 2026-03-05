from pathlib import Path

from telegram_summary_tool.telegram_export import load_telegram_export


SAMPLE_EXPORT = Path(__file__).resolve().parents[1] / "examples" / "sample-result.json"


def test_load_sample_export():
    export = load_telegram_export(SAMPLE_EXPORT)

    assert export.chat_name == "Telegram Summary Tool Demo"
    assert len(export.messages) == 5
    assert export.messages[0].author == "Alice"
    assert export.messages[-1].author == "Bob"
    assert export.messages[3].reply_to_message_id == 2
