"""API 키 암호화 저장소.

- Fernet 대칭 암호화 (AES-128-CBC + HMAC-SHA256)
- 마스터 키: 머신 고유 정보(hostname + username)에서 파생
- 암호화된 키는 로컬 파일에만 저장, .env에는 평문 미저장
- 복호화는 동일 머신에서만 가능 → 파일 유출 시에도 안전
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
from pathlib import Path
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken
from loguru import logger


VAULT_PATH = Path("config/vault.enc")
VAULT_META_PATH = Path("config/vault.meta")


def _derive_machine_key() -> bytes:
    """머신 고유 정보로 Fernet 키 파생.

    hostname + username + 고정 salt를 SHA-256 해시하여
    Fernet 호환 32바이트 키(Base64 URL-safe) 생성.
    → 같은 PC, 같은 사용자에서만 복호화 가능.
    """
    try:
        username = os.getlogin()
    except OSError:
        username = os.environ.get("USER", os.environ.get("USERNAME", "default"))
    machine_id = f"{platform.node()}:{username}:KR_STOCK_AUTOTRADER_v1"
    digest = hashlib.sha256(machine_id.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


class SecureVault:
    """암호화된 API 키 저장소."""

    def __init__(self):
        self._key = _derive_machine_key()
        self._fernet = Fernet(self._key)
        self._data: dict[str, str] = {}
        self._load()

    # ─── 공개 API ──────────────────────────────────────────

    def get(self, name: str, default: str = "") -> str:
        """키 조회 (복호화)."""
        return self._data.get(name, default)

    def set(self, name: str, value: str) -> None:
        """키 저장 (암호화)."""
        self._data[name] = value

    def set_many(self, items: dict[str, str]) -> None:
        """여러 키 한번에 저장."""
        self._data.update(items)

    def delete(self, name: str) -> None:
        """키 삭제."""
        self._data.pop(name, None)

    def save(self) -> None:
        """디스크에 암호화 저장."""
        VAULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        plaintext = json.dumps(self._data, ensure_ascii=False).encode("utf-8")
        encrypted = self._fernet.encrypt(plaintext)
        VAULT_PATH.write_bytes(encrypted)

        # 메타 파일: 어떤 키가 저장되어 있는지 (값은 마스킹)
        meta = {}
        for k, v in self._data.items():
            if v:
                meta[k] = v[:4] + "*" * max(len(v) - 8, 4) + v[-4:] if len(v) > 8 else "****"
            else:
                meta[k] = "(미설정)"
        VAULT_META_PATH.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info(f"암호화 저장소 저장 완료: {VAULT_PATH}")

    def has_keys(self) -> bool:
        """필수 키가 모두 설정되었는지 확인."""
        required = ["UPBIT_ACCESS_KEY", "UPBIT_SECRET_KEY", "GEMINI_API_KEY"]
        return all(bool(self._data.get(k)) for k in required)

    def get_status(self) -> dict[str, str]:
        """각 키의 설정 상태 (마스킹)."""
        all_keys = [
            "UPBIT_ACCESS_KEY", "UPBIT_SECRET_KEY",
            "GEMINI_API_KEY", "CLAUDE_API_KEY",
            "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD",
        ]
        status = {}
        for k in all_keys:
            v = self._data.get(k, "")
            if v:
                if len(v) > 8:
                    status[k] = v[:3] + "•" * min(len(v) - 6, 10) + v[-3:]
                else:
                    status[k] = "•" * len(v) + " (설정됨)"
            else:
                status[k] = ""
        return status

    def export_to_env(self) -> dict[str, str]:
        """os.environ에 주입 (런타임 사용용)."""
        for k, v in self._data.items():
            if v:
                os.environ[k] = v
        return dict(self._data)

    # ─── 내부 ──────────────────────────────────────────────

    def _load(self) -> None:
        """디스크에서 복호화 로드."""
        if not VAULT_PATH.exists():
            logger.info("암호화 저장소 없음 — 초기 설정 필요")
            self._data = {}
            return

        try:
            encrypted = VAULT_PATH.read_bytes()
            plaintext = self._fernet.decrypt(encrypted)
            self._data = json.loads(plaintext.decode("utf-8"))
            logger.info(f"암호화 저장소 로드 완료 ({len(self._data)}개 키)")
        except InvalidToken:
            logger.error("암호화 저장소 복호화 실패 — 다른 PC이거나 손상된 파일")
            self._data = {}
        except Exception as e:
            logger.error(f"암호화 저장소 로드 오류: {e}")
            self._data = {}
