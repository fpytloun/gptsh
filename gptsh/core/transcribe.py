"""Audio transcription support using OpenAI Whisper API.

This module provides:
- Audio transcription via OpenAI Whisper API
- Speech content detection (filters music/noise)
- Configuration management
- Error handling and graceful fallback
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

_log = logging.getLogger(__name__)

# Non-speech indicators in Whisper transcripts
_NON_SPEECH_MARKERS = {"[MUSIC]", "[NOISE]", "[SILENCE]", "[APPLAUSE]"}


def get_transcribe_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Extract and resolve transcription configuration from provider system.

    Resolves API key and base_url from the configured provider instead of
    hardcoding provider.openai.

    Returns dict with resolved settings including provider credentials.
    """
    transcribe_cfg = config.get("transcribe", {})
    providers_cfg = config.get("providers", {})

    # Resolve provider name (default to "openai" for backwards compat)
    provider_name = transcribe_cfg.get("provider", "openai")
    provider_cfg = providers_cfg.get(provider_name, {})

    # Extract API key from provider config
    # The config loader already expands ${VAR} references, so this value
    # is ready to use as-is
    api_key = provider_cfg.get("api_key")

    # Extract base_url from provider config, with sensible defaults
    base_url = provider_cfg.get("base_url")
    if not base_url:
        # Default to OpenAI API if no base_url provided
        base_url = "https://api.openai.com/v1"

    # Warn if provider not found (but only if transcription was enabled/configured)
    if provider_name not in providers_cfg and (
        transcribe_cfg.get("enabled") is True or "transcribe" in config
    ):
        _log.warning(
            "Transcription provider '%s' not found in config. "
            "Transcription may fail at runtime if API credentials are needed.",
            provider_name,
        )

    # Check if transcription should be enabled
    # Only auto-enable if:
    # 1. "transcribe" config section exists in config
    # 2. AND API key is present from provider
    # This prevents auto-enabling when transcribe isn't configured at all
    enabled = transcribe_cfg.get("enabled")
    if enabled is None:
        # Only auto-enable if transcribe section was explicitly configured
        has_transcribe_config = "transcribe" in config
        enabled = has_transcribe_config and bool(api_key)

    if not enabled and api_key is None:
        _log.debug(
            "Audio transcription disabled (no API key in provider '%s'). "
            "Audio files will be sent as attachment markers.",
            provider_name,
        )

    return {
        "enabled": enabled,
        "api_key": api_key,
        "base_url": base_url,
        "provider": provider_name,
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
    """Transcribe audio data using a configured provider's Whisper API.

    Uses the transcribe.provider configuration to resolve credentials and
    base_url from the provider system instead of hardcoding provider lookup.

    Args:
        data: Raw audio file bytes
        mime: MIME type (e.g., audio/mpeg, audio/wav)
        config: Full gptsh config dictionary

    Returns:
        Transcript text if successful and contains speech, None otherwise.
        Returns None if:
        - Transcription is disabled
        - File exceeds size limit
        - Provider not configured
        - API call fails
        - Transcript contains no speech content

    The transcript is logged at debug level for troubleshooting.
    Any errors are logged as warnings but never raise exceptions.
    """
    transcribe_cfg = get_transcribe_config(config)

    if not transcribe_cfg["enabled"]:
        _log.debug("Audio transcription disabled in config")
        return None

    api_key = transcribe_cfg["api_key"]
    if not api_key:
        _log.debug(
            "No API key for transcription provider '%s', cannot transcribe audio",
            transcribe_cfg["provider"],
        )
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

        # Use resolved base_url from provider config
        base_url = transcribe_cfg["base_url"]
        url = f"{base_url}/audio/transcriptions"

        # Prepare multipart form data
        # Note: model and other params go in the form data, not query params
        form_data = {
            "model": transcribe_cfg["model"],
            "response_format": "json",
        }
        if transcribe_cfg.get("language"):
            form_data["language"] = transcribe_cfg["language"]

        files = {
            "file": (f"audio.{ext}", data, mime),
        }

        headers = {
            "Authorization": f"Bearer {api_key}",
        }

        # Call Whisper API with timeout
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                url,
                files=files,
                data=form_data,
                headers=headers,
            )

        if response.status_code != 200:
            _log.warning(
                "Whisper API error (status %d, provider '%s'): %s",
                response.status_code,
                transcribe_cfg["provider"],
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
