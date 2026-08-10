"""
配置管理

设计约束：
- 仅 neo4j / postgres 两类数据库连接配置支持通过环境变量读取；
  若环境变量未设置，则回退到项目根目录的 config.yaml（参考 config.yaml.example）。
- 其余所有配置（含 LLM、OpenCode 等）禁止通过环境变量读取，必须写入数据库 configs 表，
  在运行期通过 `src.services.config_service` 读取。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv


@dataclass
class Neo4jConfig:
    uri: str
    user: str
    password: str


@dataclass
class PostgresConfig:
    host: str
    port: int
    db: str
    user: str
    password: str

    @property
    def sqlalchemy_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.user}:{self.password}@"
            f"{self.host}:{self.port}/{self.db}"
        )


@dataclass
class Config:
    """应用启动期可见的最小配置集合（仅含连接信息）"""

    neo4j: Neo4jConfig
    postgres: PostgresConfig
    log_level: str = "INFO"
    log_file: Optional[Path] = None
    project_root: Optional[Path] = None
    work_dir: Optional[Path] = None
    extra: Dict[str, Any] = field(default_factory=dict)


_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _default_config_yaml_path() -> Path:
    return _PROJECT_ROOT / "config.yaml"


def _load_yaml_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import yaml  # 延迟导入，避免 pyyaml 未安装时阻塞非 yaml 模式
    except Exception:
        return {}
    try:
        with path.open("r", encoding="utf-8") as fp:
            data = yaml.safe_load(fp) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _resolve(env_key: str, yaml_value: Any, default: Any) -> Any:
    v = os.getenv(env_key)
    if v is not None and v != "":
        return v
    if yaml_value not in (None, ""):
        return yaml_value
    return default


def load_config(config_path: Optional[Path] = None) -> Config:
    """加载最小启动配置：neo4j / postgres + 少量运行时参数。

    优先级：环境变量 > config.yaml > 内置默认值
    """
    load_dotenv()

    yaml_path = config_path or _default_config_yaml_path()
    yaml_data = _load_yaml_config(yaml_path)
    neo_yaml = yaml_data.get("neo4j", {}) if isinstance(yaml_data.get("neo4j"), dict) else {}
    pg_yaml = yaml_data.get("postgres", {}) if isinstance(yaml_data.get("postgres"), dict) else {}

    neo4j = Neo4jConfig(
        uri=_resolve("NEO4J_URI", neo_yaml.get("uri"), "bolt://127.0.0.1:7687"),
        user=_resolve("NEO4J_USER", neo_yaml.get("user"), "neo4j"),
        password=_resolve("NEO4J_PASSWORD", neo_yaml.get("password"), "neo4j"),
    )

    postgres = PostgresConfig(
        host=_resolve("POSTGRES_HOST", pg_yaml.get("host"), "127.0.0.1"),
        port=int(_resolve("POSTGRES_PORT", pg_yaml.get("port"), 5432)),
        db=_resolve("POSTGRES_DB", pg_yaml.get("db"), "argusmind"),
        user=_resolve("POSTGRES_USER", pg_yaml.get("user"), "argusmind"),
        password=_resolve("POSTGRES_PASSWORD", pg_yaml.get("password"), "argusmind"),
    )

    log_file_str = yaml_data.get("log_file")
    work_dir_str = yaml_data.get("work_dir")

    return Config(
        neo4j=neo4j,
        postgres=postgres,
        log_level=str(_resolve("LOG_LEVEL", yaml_data.get("log_level"), "INFO")),
        log_file=Path(log_file_str) if log_file_str else None,
        project_root=_PROJECT_ROOT,
        work_dir=Path(work_dir_str) if work_dir_str else (_PROJECT_ROOT / "work"),
        extra={k: v for k, v in yaml_data.items() if k not in {"neo4j", "postgres", "log_level", "log_file", "work_dir"}},
    )


# --------------------- 兼容旧代码 ---------------------
# 旧代码里仍有 `from src.config import LLMConfig` / `OpenCodeConfig` 的引用（agents / orchestrator）。
# 这里保留纯数据类以便平滑迁移，真正数值一律从数据库 configs 表读取。


@dataclass
class LLMConfig:
    provider: str
    api_key: str
    model: str
    type: str
    base_url: Optional[str] = None
    api_version: Optional[str] = None
    azure_endpoint: Optional[str] = None
    # API 协议格式：仅对「自定义 Provider」生效，用于路由到对应的上游端点。
    #   "openai"     -> OpenAI 兼容（/v1/chat/completions）
    #   "anthropic"  -> Anthropic 格式（/v1/messages）
    # 为空时按 base_url 启发式推断（含 /anthropic 视作 anthropic，否则 openai）。
    llm_format: Optional[str] = None


@dataclass
class OpenCodeConfig:
    base_url: str
    model_id: Optional[str] = None
    provider_id: Optional[str] = None
    system_prompt: Optional[str] = None
    session_id: Optional[str] = None
