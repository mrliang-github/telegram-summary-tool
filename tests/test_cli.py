from pathlib import Path
import subprocess
import sys


SAMPLE_EXPORT = Path(__file__).resolve().parents[1] / "examples" / "sample-result.json"


def test_cli_raw_export_stdout():
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "telegram_summary_tool.cli",
            str(SAMPLE_EXPORT),
            "--source",
            "raw-export",
            "--days",
            "7",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Telegram Group Summary" in proc.stdout
    assert "Telegram Summary Tool Demo" in proc.stdout
