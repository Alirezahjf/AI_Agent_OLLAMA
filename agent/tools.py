"""Constrained local tools. Paths never escape the configured workspace."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import re
import subprocess
from typing import Any


class ToolError(Exception):
    pass


@dataclass
class ToolResult:
    text: str
    changed: bool = False
    needs_approval: bool = False


class LocalTools:
    # Nothing is foolproof against a malicious user with shell access; these are intentional guardrails.
    HARD_BLOCKS = (
        r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f?\s+[/~]",
        r"\bmkfs\b",
        r"\bdd\s+.*\bof=/dev/",
        r":\(\)\s*\{",
        r"\bshutdown\b|\breboot\b|\bpoweroff\b",
        r"\b(chmod|chown)\s+-R\s+[/~]",
        r"\bcurl\b.*\|\s*(ba)?sh\b",
        r"\bwget\b.*\|\s*(ba)?sh\b",
    )
    READ_ONLY = re.compile(
        r"^\s*(pwd|ls|find|cat|head|tail|grep|rg|git\s+(status|diff|log|show|branch))\b"
    )

    def __init__(self, root: Path, timeout: int, max_output: int) -> None:
        self.root = root.resolve()
        self.timeout = timeout
        self.max_output = max_output

    def _path(self, value: str) -> Path:
        path = (
            (self.root / value).resolve()
            if not Path(value).is_absolute()
            else Path(value).resolve()
        )
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ToolError("دسترسی خارج از WORKSPACE_ROOT مجاز نیست.") from exc
        return path

    def list_files(self, path: str = ".", depth: int = 2) -> ToolResult:
        target = self._path(path)
        if not target.is_dir():
            raise ToolError("مسیر یک پوشه نیست.")
        depth = max(0, min(int(depth), 5))
        lines: list[str] = []
        for item in sorted(target.rglob("*")):
            if len(item.relative_to(target).parts) <= depth:
                lines.append(("📁 " if item.is_dir() else "📄 ") + str(item.relative_to(self.root)))
                if len(lines) >= 500:
                    lines.append("… (نتایج محدود شدند)")
                    break
        return ToolResult("\n".join(lines) or "پوشه خالی است.")

    def read_file(self, path: str, start_line: int = 1, end_line: int = 300) -> ToolResult:
        target = self._path(path)
        if not target.is_file():
            raise ToolError("فایل پیدا نشد.")
        if target.stat().st_size > 1_000_000:
            raise ToolError("فایل بزرگ‌تر از 1MB است.")
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise ToolError("فایل متنی UTF-8 نیست.") from exc
        start, end = max(1, int(start_line)), min(len(lines), int(end_line))
        return ToolResult(
            "\n".join(f"{i:>5} | {line}" for i, line in enumerate(lines[start - 1 : end], start))
        )

    def write_file(self, path: str, content: str) -> ToolResult:
        target = self._path(path)
        if len(content.encode()) > 1_000_000:
            raise ToolError("محتوای فایل بیش از 1MB است.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return ToolResult(f"فایل ذخیره شد: {target.relative_to(self.root)}", changed=True)

    def run_command(self, command: str, cwd: str = ".") -> ToolResult:
        if len(command) > 4000:
            raise ToolError("دستور بیش از حد طولانی است.")
        if any(re.search(pattern, command, re.I) for pattern in self.HARD_BLOCKS):
            raise ToolError("این دستور به‌دلیل خطر تخریب سیستم مسدود شد.")
        directory = self._path(cwd)
        if not directory.is_dir():
            raise ToolError("cwd یک پوشه معتبر نیست.")
        # bash is deliberate for normal developer workflows; cwd and timeout remain restricted.
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.root),
            "TERM": "dumb",
            "LANG": "C.UTF-8",
        }
        try:
            completed = subprocess.run(
                ["bash", "-lc", command],
                cwd=directory,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=self.timeout,
                check=False,
            )
            output = completed.stdout or "(بدون خروجی)"
            output = output[: self.max_output] + (
                "\n… خروجی کوتاه شد" if len(output) > self.max_output else ""
            )
            return ToolResult(
                f"$ {command}\n\n{output}\n\n[exit code: {completed.returncode}]",
                changed=not bool(self.READ_ONLY.match(command)),
            )
        except subprocess.TimeoutExpired as exc:
            output = (
                (exc.stdout or "").decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            )
            return ToolResult(
                f"$ {command}\n\nزمان دستور پس از {self.timeout} ثانیه تمام شد.\n{output[: self.max_output]}"
            )

    def invoke(self, name: str, args: dict[str, Any]) -> ToolResult:
        methods = {
            "list_files": self.list_files,
            "read_file": self.read_file,
            "write_file": self.write_file,
            "run_command": self.run_command,
        }
        if name not in methods:
            raise ToolError(f"ابزار ناشناخته: {name}")
        return methods[name](**args)

    def requires_approval(self, name: str, args: dict[str, Any]) -> bool:
        return name == "write_file" or (
            name == "run_command" and not self.READ_ONLY.match(str(args.get("command", "")))
        )
