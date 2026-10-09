#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MY_AGENT 自动快照备份脚本（纯标准库，无外部依赖）

用法:
  python scripts/backup.py                       # 默认：备份到 ./backups，滚动保留最近 10 份
  python scripts/backup.py --dest D:/myagent_snaps --keep 20
  python scripts/backup.py --no-rotate           # 不清理旧备份

设计要点（对应重构报告 §3 备份方法）:
  - 纯标准库 zipfile，跨平台、免安装
  - 排除: venv/.venv/node_modules/__pycache__/*.pyc/*.gguf/logs/backups/.workbuddy
          以及密钥文件(secrets.yaml / credentials.yaml)
  - 密钥不进备份包：重建时由环境变量或单独加密通道注入（见 REBUILD_NOTES.md）
  - 时间戳命名: myagent_backup_YYYYMMDD_HHMM.zip
  - 滚动保留: 按修改时间保留最近 --keep 份，避免磁盘被撑满
"""
import argparse
import time
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent  # scripts/.. = 项目根

# 需要备份的目录（相对项目根）
INCLUDE_DIRS = [
    "agents", "server", "providers", "utils", "config", "skills", "reference",
    "docs", "tests", "scripts", "macros", "workspace",
]
# 需要备份的顶层文件
INCLUDE_FILES = [
    "main.py", "start.py", "requirements.txt", ".gitignore",
    "companion_launcher.py", "companion_patch.py",
]

# 排除目录名（任意层级命中即跳过）
EXCLUDE_DIRS = {".git", "venv", ".venv", "node_modules", "__pycache__",
                "logs", "backups", ".workbuddy", ".idea", ".vscode"}
# 排除后缀
EXCLUDE_SUFFIX = {".pyc", ".gguf", ".wav", ".log", ".tmp", ".bak"}
# 密钥文件不进包（重建时由环境变量注入）
SECRET_FILES = {"secrets.yaml", "credentials.yaml"}


def should_exclude(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    if set(rel.parts) & EXCLUDE_DIRS:
        return True
    if "node_modules" in rel.parts:
        return True
    if path.name in SECRET_FILES:
        return True
    if path.suffix in EXCLUDE_SUFFIX:
        return True
    return False


def collect_files(root: Path):
    files = []
    for d in INCLUDE_DIRS:
        p = root / d
        if not p.exists():
            continue
        for f in p.rglob("*"):
            if f.is_file() and not should_exclude(f, root):
                files.append(f)
    for name in INCLUDE_FILES:
        p = root / name
        if p.exists() and not should_exclude(p, root):
            files.append(p)
    return files


def main():
    ap = argparse.ArgumentParser(description="MY_AGENT 快照备份")
    ap.add_argument("--dest", default=str(PROJECT_ROOT / "backups"),
                    help="备份输出目录（默认 ./backups）")
    ap.add_argument("--keep", type=int, default=10,
                    help="滚动保留份数（默认 10，0=不清理）")
    ap.add_argument("--no-rotate", action="store_true",
                    help="不清理旧备份")
    args = ap.parse_args()

    root = PROJECT_ROOT
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime("%Y%m%d_%H%M")
    out = dest / f"myagent_backup_{stamp}.zip"

    files = collect_files(root)
    if not files:
        print("[backup] 未找到可备份文件，跳过")
        return

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, f.relative_to(root))
    size_kb = out.stat().st_size // 1024
    print(f"[backup] 已备份 {len(files)} 个文件 -> {out} ({size_kb} KB)")

    if not args.no_rotate and args.keep > 0:
        zips = sorted(dest.glob("myagent_backup_*.zip"),
                      key=lambda p: p.stat().st_mtime)
        while len(zips) > args.keep:
            old = zips.pop(0)
            old.unlink()
            print(f"[backup] 清理旧备份: {old.name}")
    print("[backup] 完成")


if __name__ == "__main__":
    main()
