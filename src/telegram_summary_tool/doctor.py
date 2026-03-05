from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


MIN_PYTHON = (3, 9)
TELEGRAM_CONTAINER = Path(
    os.path.expanduser(
        "~/Library/Group Containers/6N38VWS5BX.ru.keepcoder.Telegram/appstore"
    )
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str  # ok | warn | fail
    detail: str
    suggestion: Optional[str] = None


def _which(name: str) -> Optional[str]:
    return shutil.which(name)


def _command_version(cmd: str) -> Optional[str]:
    path = _which(cmd)
    if not path:
        return None
    try:
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except Exception:
        return path

    output = (proc.stdout or proc.stderr).strip().splitlines()
    if output:
        return f"{path} ({output[0].strip()})"
    return path


def _check_python() -> CheckResult:
    version = sys.version_info
    current = f"{version.major}.{version.minor}.{version.micro}"
    if (version.major, version.minor) < MIN_PYTHON:
        return CheckResult(
            name="Python",
            status="fail",
            detail=f"当前版本 {current}，要求 >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
            suggestion="安装较新 Python 后重新创建虚拟环境。",
        )
    return CheckResult(name="Python", status="ok", detail=current)


def _check_command(name: str, required_for: str, required: bool) -> CheckResult:
    version_info = _command_version(name)
    if version_info:
        return CheckResult(name=name, status="ok", detail=version_info)

    status = "fail" if required else "warn"
    return CheckResult(
        name=name,
        status=status,
        detail=f"未找到可执行文件（用于 {required_for}）",
        suggestion=f"请安装 `{name}` 并确保在 PATH 中。",
    )


def _check_telegram_container() -> CheckResult:
    if platform.system() != "Darwin":
        return CheckResult(
            name="Telegram local data",
            status="warn",
            detail="local 模式仅支持 macOS App Store 版 Telegram。",
            suggestion="如需跨平台，请使用 raw-export 或 tgmix 模式。",
        )

    if TELEGRAM_CONTAINER.exists():
        return CheckResult(
            name="Telegram local data",
            status="ok",
            detail=f"已找到: {TELEGRAM_CONTAINER}",
        )
    return CheckResult(
        name="Telegram local data",
        status="warn",
        detail=f"未找到目录: {TELEGRAM_CONTAINER}",
        suggestion="先启动并登录 App Store 版 Telegram 再重试。",
    )


def _mode_status(results: dict[str, CheckResult]) -> list[CheckResult]:
    python_ok = results["Python"].status == "ok"
    uvx_ok = results["uvx"].status == "ok"
    sqlcipher_ok = results["sqlcipher"].status == "ok"
    local_data_ok = results["Telegram local data"].status == "ok"
    ai_ok = results["claude"].status == "ok" or results["codex"].status == "ok"

    mode_checks = [
        (
            "Mode raw-export",
            "ok" if python_ok else "fail",
            "可用" if python_ok else "不可用：Python 版本不满足要求",
            "安装 Python >= 3.9。",
        ),
        (
            "Mode tgmix",
            "ok" if python_ok and uvx_ok else "warn",
            "可用" if python_ok and uvx_ok else "缺少 uvx，无法运行 tgmix 预处理",
            "安装 uv: https://docs.astral.sh/uv/",
        ),
        (
            "Mode local (macOS)",
            "ok" if python_ok and sqlcipher_ok and local_data_ok else "warn",
            (
                "可用"
                if python_ok and sqlcipher_ok and local_data_ok
                else "需要 sqlcipher + 本地 Telegram 数据目录"
            ),
            "安装 sqlcipher，并确认 App Store 版 Telegram 已登录。",
        ),
        (
            "AI summary",
            "ok" if ai_ok else "warn",
            "可用" if ai_ok else "未检测到 claude/codex CLI",
            "安装并登录 Claude Code 或 Codex CLI。",
        ),
    ]

    return [
        CheckResult(name=name, status=status, detail=detail, suggestion=suggestion)
        for name, status, detail, suggestion in mode_checks
    ]


def run_doctor() -> list[CheckResult]:
    checks: dict[str, CheckResult] = {
        "Python": _check_python(),
        "uvx": _check_command("uvx", "tgmix 模式", required=False),
        "sqlcipher": _check_command("sqlcipher", "local 模式", required=False),
        "node": _check_command("node", "GramJS 连接器", required=False),
        "npm": _check_command("npm", "GramJS 连接器", required=False),
        "claude": _check_command("claude", "AI 智能分析", required=False),
        "codex": _check_command("codex", "AI 智能分析", required=False),
        "Telegram local data": _check_telegram_container(),
    }
    return list(checks.values()) + _mode_status(checks)


def _status_tag(status: str) -> str:
    if status == "ok":
        return "[ OK ]"
    if status == "fail":
        return "[FAIL]"
    return "[WARN]"


def _print_text(results: list[CheckResult]) -> None:
    print("Telegram Summary Tool - Environment Doctor")
    print("")
    for item in results:
        print(f"{_status_tag(item.status)} {item.name}: {item.detail}")
        if item.suggestion and item.status != "ok":
            print(f"       -> {item.suggestion}")

    fails = sum(1 for x in results if x.status == "fail")
    warns = sum(1 for x in results if x.status == "warn")
    print("")
    print(f"Summary: {len(results)} checks, {fails} fail, {warns} warn")
    if fails == 0:
        print("Core environment is ready for at least raw-export mode.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tg-doctor",
        description="Check local environment readiness for telegram-summary-tool.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output check results as JSON.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    results = run_doctor()

    if args.json:
        print(
            json.dumps(
                [asdict(item) for item in results],
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    _print_text(results)

    if any(item.status == "fail" for item in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
