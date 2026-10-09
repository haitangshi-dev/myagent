"""技能安装目录的共享 I/O —— 仅依赖标准库，避免任何循环依赖。

目标：把已安装技能存到「用户数据目录」而非打包目录，使更新 exe 后技能不丢失，
且无需重新打包即可在运行时加载并使用技能（配合 tools.list_skills / tools.use_skill）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def skill_install_root() -> Path:
    """返回技能安装根目录。

    优先级：
      1. 环境变量 MY_AGENT_SKILLS_DIR（绝对路径，由 Electron 壳注入到 userData/skills）
      2. 回落到 <项目根>/skills（dev 模式，行为同原实现）

    首次调用时（安装态）会把打包自带的「内置 skill」（resources/backend/skills/）
    一次性复制到用户数据目录，保证更新 exe 后内置技能不丢、不污染安装目录。
    """
    env = (os.environ.get("MY_AGENT_SKILLS_DIR") or "").strip()
    if env:
        p = Path(env)
        if p.is_absolute():
            _seed_builtin_skills(p)
            return p
    return Path(__file__).resolve().parent.parent / "skills"


def _seed_builtin_skills(target: Path) -> None:
    """把打包内置的 skills/ 里的内置技能复制到 userData（幂等：目标已有则跳过）。"""
    try:
        builtin = Path(__file__).resolve().parent.parent / "skills"
        if not builtin.is_dir():
            return
        target.mkdir(parents=True, exist_ok=True)
        # installed.json 合并
        t_inst = target / "installed.json"
        b_inst = builtin / "installed.json"
        if b_inst.exists():
            try:
                import json as _json

                t_data = (
                    _json.loads(t_inst.read_text(encoding="utf-8"))
                    if t_inst.exists()
                    else {}
                )
                b_data = _json.loads(b_inst.read_text(encoding="utf-8"))
                merged = {**b_data, **t_data}  # userData 已有条目优先（可被覆盖/升级）
                t_inst.write_text(
                    _json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception:  # noqa: BLE001
                pass
        # 内置技能目录复制（SKILL.md 等）
        for item in builtin.iterdir():
            if not item.is_dir():
                continue
            dest = target / item.name
            if dest.exists():
                continue  # 已存在（用户可能改过），不覆盖
            try:
                import shutil

                shutil.copytree(item, dest, dirs_exist_ok=True)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass


def installed_path() -> Path:
    return skill_install_root() / "installed.json"


def read_installed() -> dict[str, Any]:
    """读取 installed.json（已安装技能清单）；不存在或损坏返回 {}。"""
    p = installed_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def write_installed(data: dict[str, Any]) -> None:
    """把已安装技能清单写回 installed.json，确保父目录存在。"""
    root = skill_install_root()
    root.mkdir(parents=True, exist_ok=True)
    installed_path().write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_skill(slug: str) -> tuple[Path, str, list[str]]:
    """读取已安装技能的 SKILL.md 全文及附带文件列表。

    返回 (skill_dir, markdown, extra_files)：
      - skill_dir  : 技能目录绝对路径（即使未安装也返回预期路径）
      - markdown   : SKILL.md 全文（不存在则为空串）
      - extra_files: 同目录下除 SKILL.md 外的其它文件名（已排序），供模型决定是否用 shell 执行

    注意：本函数只「读取并返回说明文本」，绝不自动执行任何脚本。
    真正执行由模型按说明用现有 shell 工具完成（受保护目录硬拦截）。
    """
    root = skill_install_root()
    skill_dir = root / slug
    markdown = ""
    skill_md = skill_dir / "SKILL.md"
    if skill_md.exists():
        try:
            markdown = skill_md.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            markdown = ""
    extra_files: list[str] = []
    if skill_dir.is_dir():
        for item in sorted(skill_dir.iterdir()):
            if item.name == "SKILL.md":
                continue
            extra_files.append(item.name)
    return skill_dir, markdown, extra_files
