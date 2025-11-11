"""Audio transcription support using OpenAI Whisper API.

This module provides:
- Audio transcription via OpenAI Whisper API
- Speech content detection (filters music/noise)
- Configuration management
- Error handling and graceful fallback
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import httpx

_log = logging.getLogger(__name__)

# Non-speech indicators in Whisper transcripts
_NON_SPEECH_MARKERS = {"[MUSIC]", "[NOISE]", "[SILENCE]", "[APPLAUSE]"}


def get_transcribe_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Extract and resolve transcription configuration.

    Returns dict with resolved settings including API key detection.
    """
    transcribe_cfg = config.get("transcribe", {})

    # Get OpenAI API key
    api_key_env = transcribe_cfg.get("api_key_env", "OPENAI_API_KEY")
    api_key = os.environ.get(api_key_env)

    # Check if transcription should be enabled
    # Default to True if API key is present, otherwise False
    enabled = transcribe_cfg.get("enabled")
    if enabled is None:
        enabled = bool(api_key)

    if not enabled and api_key is None:
        _log.debug(
            "Audio transcription disabled (%s not set). "
            "Audio files will be sent as attachment markers.",
            api_key_env,
        )

    return {
        "enabled": enabled,
        "api_key": api_key,
        "provider": transcribe_cfg.get("provider", "openai"),
        "model": transcribe_cfg.get("model", "whisper-1"),
        "language": transcribe_cfg.get("language"),
        "max_file_size": transcribe_cfg.get("max_file_size", 25000000),  # 25 MB
        "detect_non_speech": transcribe_cfg.get("detect_non_speech", True),
        "fallback_to_attachment": transcribe_cfg.get("fallback_to_attachment", True),
    }


def is_speech_content(transcript: str) -> bool:
    """Detect if transcript contains actual speech vs music/noise.

    Heuristics:
    - Check for non-speech markers ([MUSIC], [NOISE], etc.)
    - Very short transcripts (< 10 chars) likely noise
    - Mostly punctuation/symbols indicates poor transcription

    Args:
        transcript: Transcribed text

    Returns:
        False if likely non-speech, True if likely speech or uncertain
    """
    if not transcript or not transcript.strip():
        return False

    # Check for explicit non-speech markers
    upper_text = transcript.upper().strip()
    if any(marker in upper_text for marker in _NON_SPEECH_MARKERS):
        _log.debug(
            "Detected non-speech content in transcript: %s",
            transcript[:100],
        )
        return False

    # Very short transcripts are likely noise/clicks/silence
    if len(transcript.strip()) < 10:
        _log.debug(
            "Transcript too short (likely noise): %s",
            transcript[:100],
        )
        return False

    # Count meaningful characters vs punctuation/symbols
    meaningful = sum(1 for c in transcript if c.isalnum() or c.isspace() or c in ",.!?;:-")
    ratio = meaningful / len(transcript) if transcript else 0

    if ratio < 0.5:
        _log.debug(
            "Transcript mostly symbols (ratio=%.2f): %s",
            ratio,
            transcript[:100],
        )
        return False

    return True


async def transcribe_audio(
    data: bytes,
    mime: str,
    config: Dict[str, Any],
) -> Optional[str]:
    """Transcribe audio data using OpenAI Whisper API.

    Args:
        data: Raw audio file bytes
        mime: MIME type (e.g., audio/mpeg, audio/wav)
        config: Full gptsh config dictionary

    Returns:
        Transcript text if successful and contains speech, None otherwise.
        Returns None if:
        - Transcription is disabled
        - File exceeds size limit
        - API call fails
        - Transcript contains no speech content
        - Non-OpenAI provider configured (future)

    The transcript is logged at debug level for troubleshooting.
    Any errors are logged as warnings but never raise exceptions.
    """
    transcribe_cfg = get_transcribe_config(config)

    if not transcribe_cfg["enabled"]:
        _log.debug("Audio transcription disabled in config")
        return None

    if transcribe_cfg["provider"] != "openai":
        _log.warning(
            "Transcription provider '%s' not yet supported",
            transcribe_cfg["provider"],
        )
        return None

    api_key = transcribe_cfg["api_key"]
    if not api_key:
        _log.debug("OPENAI_API_KEY not set, cannot transcribe audio")
        return None

    # Check file size
    max_size = transcribe_cfg["max_file_size"]
    if len(data) > max_size:
        _log.warning(
            "Audio file too large for transcription: %d bytes (max %d)",
            len(data),
            max_size,
        )
        return None

    try:
        # Determine file extension from MIME type for multipart form
        mime_to_ext = {
            "audio/mpeg": "mp3",
            "audio/wav": "wav",
            "audio/ogg": "ogg",
            "audio/flac": "flac",
            "audio/m4a": "m4a",
            "audio/aac": "aac",
            "audio/webm": "webm",
        }
        ext = mime_to_ext.get(mime, mime.split("/")[-1] or "audio")

        # Get base URL from provider config, default to OpenAI
        providers_cfg = config.get("providers", {})
        openai_cfg = providers_cfg.get("openai", {})
        base_url = openai_cfg.get("base_url") or "https://api.openai.com/v1"

        url = f"{base_url}/audio/transcriptions"

        # Prepare multipart form data
        files = {
            "file": (f"audio.{ext}", data, mime),
        }
        params = {
            "model": transcribe_cfg["model"],
            "response_format": "json",
        }
        if transcribe_cfg.get("language"):
            params["language"] = transcribe_cfg["language"]

        headers = {
            "Authorization": f"Bearer {api_key}",
        }

        # Call OpenAI Whisper API with timeout
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                url,
                files=files,
                params=params,
                headers=headers,
            )

        if response.status_code != 200:
            _log.warning(
                "OpenAI Whisper API error (status %d): %s",
                response.status_code,
                response.text[:200],
            )
            return None

        result = response.json()
        transcript = result.get("text", "").strip()

        if not transcript:
            _log.debug("Whisper API returned empty transcript")
            return None

        # Check for meaningful speech content
        if transcribe_cfg["detect_non_speech"]:
            if not is_speech_content(transcript):
                _log.debug("Detected non-speech audio content, falling back to marker")
                return None

        _log.debug("Successfully transcribed audio: %s", transcript[:100])
        return transcript

    except httpx.TimeoutException:
        _log.warning("Whisper API call timed out")
        return None
    except httpx.RequestError as e:
        _log.warning("Whisper API request failed: %s", e)
        return None
    except Exception as e:
        _log.warning("Unexpected error during transcription: %s", e)
        return None
