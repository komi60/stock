"""설정 로드 및 관리.

우선순위: 암호화 저장소(vault) → .env → 환경변수
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from loguru import logger
from pydantic import BaseModel, Field


def load_settings(
    yaml_path: str = "config/settings.yaml",
    env_path: str = "config/.env",
) -> dict[str, Any]:
    """YAML 설정 + .env 환경변수를 로드."""
    env_file = Path(env_path)
    if env_file.exists():
        load_dotenv(env_file)

    yaml_file = Path(yaml_path)
    if not yaml_file.exists():
        raise FileNotFoundError(f"설정 파일을 찾을 수 없습니다: {yaml_path}")

    with open(yaml_file, "r", encoding="utf-8") as f:
        settings = yaml.safe_load(f)

    return settings


def _load_vault_to_env() -> None:
    """암호화 저장소의 키를 os.environ에 주입 (최우선)."""
    try:
        from core.security import SecureVault
        vault = SecureVault()
        if vault.has_keys():
            vault.export_to_env()
            logger.info("암호화 저장소에서 API 키 로드 완료")
    except Exception as e:
        logger.debug(f"암호화 저장소 로드 스킵: {e}")


class GeminiConfig(BaseModel):
    """Gemini API 설정."""
    api_key: str = Field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    model: str = "gemini-2.5-flash"


class ClaudeConfig(BaseModel):
    """Claude API 설정 (보조)."""
    api_key: str = Field(default_factory=lambda: os.getenv("CLAUDE_API_KEY", ""))
    model: str = "claude-sonnet-4-20250514"


class EmailConfig(BaseModel):
    """Gmail SMTP 설정."""
    address: str = Field(default_factory=lambda: os.getenv("GMAIL_ADDRESS", ""))
    app_password: str = Field(default_factory=lambda: os.getenv("GMAIL_APP_PASSWORD", ""))
    smtp_server: str = "smtp.gmail.com"
    smtp_port: int = 587


class AppConfig(BaseModel):
    """전체 앱 설정 통합."""
    gemini: GeminiConfig = Field(default_factory=GeminiConfig)
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    settings: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def load(cls) -> "AppConfig":
        settings = load_settings()
        # 암호화 저장소 → 환경변수 주입 (최우선)
        _load_vault_to_env()
        return cls(settings=settings)
