"""
SimpleClaw v2.1 - Frozen Manifest
====================================
Ground truth imutável. Carregado no boot, congelado com MappingProxyType.
Hash SHA-256 verificado em toda operação de leitura.
Tentativa de modificação levanta ImmutableError.
Tentativa de tamper no arquivo YAML levanta SecurityError.
"""

from __future__ import annotations

import hashlib
import json
import types
from pathlib import Path
from typing import Any, Optional

import structlog
import yaml

logger = structlog.get_logger()

MANIFEST_PATH = Path(__file__).parent / "system_manifest.yaml"


class ImmutableError(Exception):
    """Raised when attempting to modify frozen manifest."""
    pass


class SecurityError(Exception):
    """Raised when manifest file tampering is detected."""
    pass


class FrozenManifest:
    """
    Ground truth imutável. Singleton.
    Carregado uma vez, hash verificado, atributos congelados.
    """

    _instance: Optional[FrozenManifest] = None
    _frozen: bool = False

    def __new__(cls, path: Optional[Path] = None):
        if cls._instance is None:
            cls._instance = object.__new__(cls)
            cls._instance._load_and_freeze(path or MANIFEST_PATH)
        return cls._instance

    def _load_and_freeze(self, path: Path) -> None:
        if not path.exists():
            raise FileNotFoundError(f"SystemManifest not found: {path}")

        with open(path, "r", encoding="utf-8") as f:
            raw_content = f.read()

        data = yaml.safe_load(raw_content)

        # Hash do conteúdo original pra detecção de tamper
        object.__setattr__(
            self, "_content_hash",
            hashlib.sha256(
                json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
        )
        object.__setattr__(self, "_manifest_path", path)
        object.__setattr__(self, "_raw_data", data)

        # Congela todos os atributos do manifest
        frozen_data = self._deep_freeze(data)
        object.__setattr__(self, "_data", frozen_data)

        # Atalhos de acesso rápido
        object.__setattr__(self, "_identity", frozen_data.get("identity", types.MappingProxyType({})))
        object.__setattr__(self, "_capabilities", frozen_data.get("capabilities", types.MappingProxyType({})))
        object.__setattr__(self, "_agents", frozen_data.get("agents", ()))
        object.__setattr__(self, "_limits", frozen_data.get("limits", types.MappingProxyType({})))

        object.__setattr__(self, "_frozen", True)

        logger.info(
            "manifest.loaded_and_frozen",
            version=data.get("identity", {}).get("version", "unknown"),
            hash=self._content_hash[:12],
        )

    def _deep_freeze(self, obj: Any) -> Any:
        """Recursivamente congela estruturas."""
        if isinstance(obj, dict):
            return types.MappingProxyType({
                k: self._deep_freeze(v) for k, v in obj.items()
            })
        elif isinstance(obj, list):
            return tuple(self._deep_freeze(i) for i in obj)
        return obj

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_frozen", False):
            raise ImmutableError(
                f"Manifest is immutable. Attempted to set '{name}' = {value!r}"
            )
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if getattr(self, "_frozen", False):
            raise ImmutableError(
                f"Manifest is immutable. Attempted to delete '{name}'"
            )
        object.__delattr__(self, name)

    def verify_integrity(self) -> bool:
        """
        Verifica se o arquivo YAML foi modificado em disco.
        Chamado em todo get() para garantir ground truth.
        """
        try:
            with open(self._manifest_path, "r", encoding="utf-8") as f:
                current_content = f.read()

            current_data = yaml.safe_load(current_content)
            current_hash = hashlib.sha256(
                json.dumps(current_data, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()

            if current_hash != self._content_hash:
                logger.critical(
                    "manifest.tampering_detected",
                    expected_hash=self._content_hash[:12],
                    current_hash=current_hash[:12],
                )
                return False

            return True
        except Exception as e:
            logger.error("manifest.integrity_check_failed", error=str(e))
            return False

    def get(self, path: str) -> Any:
        """
        Acesso tipado e seguro. Verifica integridade em toda chamada.
        Falha explicitamente se chave não existe — nunca retorna None.

        Args:
            path: Dot-separated path. Ex: "identity.version", "limits.max_audio_duration_seconds"
        """
        if not self.verify_integrity():
            raise SecurityError(
                "Manifest tampering detected! Sistema recusa operar. "
                "Restaure system_manifest.yaml original e reinicie."
            )

        keys = path.split(".")
        value = self._data

        for key in keys:
            if isinstance(value, types.MappingProxyType):
                if key not in value:
                    raise KeyError(f"Manifest path '{path}' not found at key '{key}'")
                value = value[key]
            elif isinstance(value, tuple):
                try:
                    idx = int(key)
                    value = value[idx]
                except (ValueError, IndexError):
                    raise KeyError(f"Manifest path '{path}': cannot index tuple with '{key}'")
            else:
                raise KeyError(f"Manifest path '{path}': reached leaf at '{key}'")

        return value

    # ─── CONVENIENCE ACCESSORS ───────────────────────────────

    @property
    def identity(self) -> types.MappingProxyType:
        if not self.verify_integrity():
            raise SecurityError("Manifest tampering detected!")
        return self._identity

    @property
    def capabilities(self) -> types.MappingProxyType:
        if not self.verify_integrity():
            raise SecurityError("Manifest tampering detected!")
        return self._capabilities

    @property
    def agents(self) -> tuple:
        if not self.verify_integrity():
            raise SecurityError("Manifest tampering detected!")
        return self._agents

    @property
    def limits(self) -> types.MappingProxyType:
        if not self.verify_integrity():
            raise SecurityError("Manifest tampering detected!")
        return self._limits

    @property
    def content_hash(self) -> str:
        return self._content_hash

    # ─── RESET (only for testing) ────────────────────────────

    @classmethod
    def _reset(cls) -> None:
        """Reset singleton. USE ONLY IN TESTS."""
        cls._instance = None
        cls._frozen = False
