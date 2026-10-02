"""配置加载与热加载。

合并加载 settings / models / roles / pipeline 四个 YAML，
顶层产出 keys：app / models / roles / pipeline / context / logging / error_handling。

相对路径（如 ./memory/dialog.md）会解析为项目根下的绝对路径。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# 项目根：src/infra/config.py -> parents[2] == 项目根 (think-core-agent)
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
CONFIG_DIR: Path = PROJECT_ROOT / "config"

_LOADED: dict[str, Any] | None = None


# stage_FE5：测试版 profile -> 隔离配置（端口 + 会话目录）。
# 在配置加载源头应用，保证 server.py 模块 import 构建 orchestrator 时即已生效。
PROFILE_OVERRIDES: dict[str, dict[str, dict[str, Any]]] = {
    "staging": {
        "app": {"port": 8002},
        "context": {"sessions_dir": "./agent-memory-staging/sessions"},
    },
}


def resolve_profile() -> str:
    """解析当前 profile：APP_PROFILE 环境变量，缺省 production。"""
    return os.environ.get("APP_PROFILE", "production").strip().lower() or "production"


def apply_profile(config: dict[str, Any]) -> str:
    """按 profile 覆写配置（须在 _resolve_context_paths 之前调用），返回生效 profile 名。"""
    profile = resolve_profile()
    if profile in PROFILE_OVERRIDES:
        for section, kv in PROFILE_OVERRIDES[profile].items():
            config.setdefault(section, {}).update(kv)
    return profile


def _resolve(p: str | Path) -> Path:
    """将相对路径解析为项目根下的绝对路径；绝对路径原样返回。"""
    p = Path(p)
    if p.is_absolute():
        return p
    return (PROJECT_ROOT / p).resolve()


def _resolve_context_paths(cfg: dict[str, Any]) -> None:
    """把 context / storage / logging 中的相对路径解析为绝对路径。"""
    ctx = cfg.setdefault("context", {})
    if "dialog_file" in ctx:
        ctx["dialog_file"] = str(_resolve(ctx["dialog_file"]))
    if "info_file" in ctx:
        ctx["info_file"] = str(_resolve(ctx["info_file"]))
    if "sessions_dir" in ctx:
        ctx["sessions_dir"] = str(_resolve(ctx["sessions_dir"]))
    # B3-0: 删除 dialog_read_rounds / info_read_rounds setdefault（死配置，全项目无读取方）

    storage = cfg.setdefault("storage", {})
    if "workspace_dir" in storage:
        storage["workspace_dir"] = str(_resolve(storage["workspace_dir"]))

    log = cfg.setdefault("logging", {})
    if "log_dir" in log:
        log["log_dir"] = str(_resolve(log["log_dir"]))


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config(config_dir: Path | str = CONFIG_DIR) -> dict[str, Any]:
    """读取并合并四个 YAML，返回顶层配置字典。"""
    config_dir = Path(config_dir)
    settings = _load_yaml(config_dir / "settings.yaml")
    models = _load_yaml(config_dir / "models.yaml")
    roles = _load_yaml(config_dir / "roles.yaml")
    pipeline = _load_yaml(config_dir / "pipeline.yaml")

    # 加载 .env（密钥走环境变量，不硬编码）
    env_file = settings.get("env_file", ".env")
    load_dotenv(str(_resolve(env_file)), override=False)

    merged: dict[str, Any] = {
        "app": settings.get("app", {}),
        # 关联读取架构修正 v3：补回 storage（此前 merged 漏了它，导致 server.py 读不到
        # allow_read_anywhere，默认 False——模型读解析出的绝对路径被白名单挡住，
        # 实测复现 fileio_read_absolute_blocked 且 abs_path 为空串被误记入 read_paths）。
        "storage": settings.get("storage", {}),
        "models": models.get("models", {}),
        # 网关重试分级（提示词18复核补漏）：merged 是白名单制，不加这行
        # models.yaml 顶层 retry_grading 节进不了运行时 config，
        # enabled:false 一键回退开关会失效（同 knowledge_dir 一类的坑）
        "retry_grading": models.get("retry_grading", {}),
        "roles": roles.get("roles", []),
        "pipeline": pipeline.get("pipeline", {}),
        "modes": pipeline.get("modes", {}),
        "context": pipeline.get("context", {}),
        "logging": pipeline.get("logging", {}),
        "error_handling": pipeline.get("error_handling", {}),
        "condenser_pipeline": pipeline.get("condenser_pipeline", {}),
        # 多任务并发调度（提示词21）：merged 是白名单制，不加这行
        # concurrency 节进不了运行时 config，enabled:false 一键回退开关会失效
        # （同 knowledge_dir / retry_grading 一类坑，务必显式加）
        "concurrency": pipeline.get("concurrency", {}),
        # 沙箱命令执行器（提示词23）：merged 是白名单制，不加这行
        # sandbox 节进不了运行时 config，后续任务书接线时读不到（同上述一类坑）
        "sandbox": settings.get("sandbox", {}),
    }

    # stage_FE5：先按 profile 覆写（sessions_dir/port），再解析为绝对路径
    apply_profile(merged)
    _resolve_context_paths(merged)
    return merged


def get_config() -> dict[str, Any]:
    """返回缓存的配置（首次调用时加载）。"""
    global _LOADED
    if _LOADED is None:
        _LOADED = load_config()
    return _LOADED


def reload_config() -> dict[str, Any]:
    """热加载：重新读取所有配置文件。"""
    global _LOADED
    _LOADED = load_config()
    return _LOADED
