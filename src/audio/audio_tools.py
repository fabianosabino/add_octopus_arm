"""
SimpleClaw v2.1 - Audio Tools
================================
Whisper (via API Groq) para transcrição de áudio recebido no Telegram.
Piper TTS (local) para sintetizar respostas em voz.

Integrados como tools no fluxo do Telegram, não como serviço separado.

Dependências:
    pip install httpx piper-tts  (piper é opcional, fallback gracioso)
    Piper model: pt_BR-faber-medium (baixado automaticamente)

Configuração .env:
    SIMPLECLAW_WHISPER_PROVIDER=groq    # ou openai
    SIMPLECLAW_WHISPER_API_KEY=gsk_...  # mesma key do Groq
    SIMPLECLAW_TTS_ENABLED=true
    SIMPLECLAW_TTS_VOICE=pt_BR-faber-medium
"""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import httpx
import structlog

from src.config.settings import get_settings

logger = structlog.get_logger()


# ─── WHISPER TRANSCRIPTION ───────────────────────────────────

async def transcribe_audio(audio_path: Path) -> dict:
    """
    Transcribe audio file using Whisper via API.

    Args:
        audio_path: Path to audio file (ogg, mp3, wav, webm)

    Returns:
        {
            "text": "transcribed text",
            "language": "pt",
            "duration": 12.5,
            "success": True,
            "error": None,
        }
    """
    settings = get_settings()
    api_key = getattr(settings, "whisper_api_key", None) or settings.router_api_key
    provider = getattr(settings, "whisper_provider", "groq")

    if not api_key:
        return {
            "text": "",
            "language": "",
            "duration": 0,
            "success": False,
            "error": "Whisper API key não configurada (SIMPLECLAW_WHISPER_API_KEY)",
        }

    if not audio_path.exists():
        return {
            "text": "",
            "language": "",
            "duration": 0,
            "success": False,
            "error": f"Arquivo não encontrado: {audio_path}",
        }

    # Convert to format Whisper accepts if needed
    converted = await _ensure_compatible_format(audio_path)
    target = converted or audio_path

    try:
        if provider == "groq":
            return await _transcribe_groq(target, api_key)
        elif provider == "openai":
            return await _transcribe_openai(target, api_key)
        else:
            return {
                "text": "",
                "success": False,
                "error": f"Provider de transcrição não suportado: {provider}",
            }
    except Exception as e:
        logger.error("audio.transcribe_failed", error=str(e))
        return {
            "text": "",
            "success": False,
            "error": str(e),
        }
    finally:
        # Cleanup converted file
        if converted and converted != audio_path and converted.exists():
            converted.unlink(missing_ok=True)


async def _transcribe_groq(audio_path: Path, api_key: str) -> dict:
    """Transcribe via Groq Whisper API."""
    async with httpx.AsyncClient(timeout=30) as client:
        with open(audio_path, "rb") as f:
            response = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (audio_path.name, f, "audio/mpeg")},
                data={
                    "model": "whisper-large-v3-turbo",
                    "language": "pt",
                    "response_format": "verbose_json",
                },
            )

    if response.status_code != 200:
        return {
            "text": "",
            "success": False,
            "error": f"Groq Whisper API error {response.status_code}: {response.text[:200]}",
        }

    data = response.json()
    return {
        "text": data.get("text", ""),
        "language": data.get("language", "pt"),
        "duration": data.get("duration", 0),
        "success": True,
        "error": None,
    }


async def _transcribe_openai(audio_path: Path, api_key: str) -> dict:
    """Transcribe via OpenAI Whisper API."""
    async with httpx.AsyncClient(timeout=30) as client:
        with open(audio_path, "rb") as f:
            response = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (audio_path.name, f, "audio/mpeg")},
                data={
                    "model": "whisper-1",
                    "language": "pt",
                    "response_format": "verbose_json",
                },
            )

    if response.status_code != 200:
        return {
            "text": "",
            "success": False,
            "error": f"OpenAI Whisper error {response.status_code}: {response.text[:200]}",
        }

    data = response.json()
    return {
        "text": data.get("text", ""),
        "language": data.get("language", "pt"),
        "duration": data.get("duration", 0),
        "success": True,
        "error": None,
    }


async def _ensure_compatible_format(audio_path: Path) -> Optional[Path]:
    """Convert audio to mp3 if needed (ffmpeg)."""
    suffix = audio_path.suffix.lower()
    if suffix in (".mp3", ".wav", ".m4a", ".flac"):
        return None  # Already compatible

    # Convert ogg/webm to mp3 via ffmpeg
    output = audio_path.with_suffix(".mp3")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", str(audio_path), "-y",
            "-vn", "-ar", "16000", "-ac", "1", "-b:a", "64k",
            str(output),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=10)
        if output.exists():
            return output
    except Exception as e:
        logger.warning("audio.convert_failed", error=str(e))

    return None


# ─── PIPER TTS ───────────────────────────────────────────────

async def synthesize_speech(text: str, output_path: Optional[Path] = None) -> dict:
    """
    Synthesize text to speech using Piper TTS (local, offline).

    Args:
        text: Text to synthesize (max ~500 chars for Telegram voice)
        output_path: Where to save WAV file. Auto-generated if None.

    Returns:
        {
            "audio_path": Path,
            "duration_seconds": float,
            "success": True,
            "error": None,
        }
    """
    settings = get_settings()
    tts_enabled = getattr(settings, "tts_enabled", False)

    if not tts_enabled:
        return {
            "audio_path": None,
            "success": False,
            "error": "TTS desabilitado (SIMPLECLAW_TTS_ENABLED=false)",
        }

    if not text or not text.strip():
        return {"audio_path": None, "success": False, "error": "Texto vazio"}

    # Truncate for reasonable voice message length
    if len(text) > 500:
        text = text[:497] + "..."

    if output_path is None:
        output_path = Path(tempfile.mktemp(suffix=".wav", prefix="simpleclaw_tts_"))

    voice = getattr(settings, "tts_voice", "pt_BR-faber-medium")

    try:
        # Try piper CLI
        proc = await asyncio.create_subprocess_exec(
            "piper",
            "--model", voice,
            "--output_file", str(output_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=text.encode("utf-8")),
            timeout=15,
        )

        if proc.returncode != 0:
            error_msg = stderr.decode("utf-8", errors="replace")[:200] if stderr else "Unknown error"
            return {"audio_path": None, "success": False, "error": f"Piper TTS falhou: {error_msg}"}

        if output_path.exists() and output_path.stat().st_size > 0:
            # Estimate duration (rough: 22050 Hz, 16-bit mono)
            size = output_path.stat().st_size
            duration = size / (22050 * 2)  # bytes / (sample_rate * bytes_per_sample)

            return {
                "audio_path": output_path,
                "duration_seconds": round(duration, 1),
                "success": True,
                "error": None,
            }
        else:
            return {"audio_path": None, "success": False, "error": "Piper não gerou arquivo"}

    except FileNotFoundError:
        return {
            "audio_path": None,
            "success": False,
            "error": "Piper TTS não instalado. Execute: pip install piper-tts",
        }
    except asyncio.TimeoutError:
        return {"audio_path": None, "success": False, "error": "TTS timeout (>15s)"}
    except Exception as e:
        return {"audio_path": None, "success": False, "error": str(e)}


# ─── CONVENIENCE ─────────────────────────────────────────────

async def convert_wav_to_ogg(wav_path: Path) -> Optional[Path]:
    """Convert WAV to OGG Opus for Telegram voice message."""
    ogg_path = wav_path.with_suffix(".ogg")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", str(wav_path), "-y",
            "-c:a", "libopus", "-b:a", "64k",
            str(ogg_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=10)
        if ogg_path.exists():
            return ogg_path
    except Exception as e:
        logger.warning("audio.ogg_convert_failed", error=str(e))
    return None
