"""Unit tests for audio transcription support."""

from __future__ import annotations

import os
from unittest import mock

import pytest

from gptsh.core.transcribe import (
    get_transcribe_config,
    is_speech_content,
    transcribe_audio,
)


class TestGetTranscribeConfig:
    """Test transcription configuration resolution."""

    def test_enabled_when_api_key_present(self):
        """Transcription should be enabled when OPENAI_API_KEY is set."""
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-key"}):
            config = {"transcribe": {}}
            result = get_transcribe_config(config)
            assert result["enabled"] is True
            assert result["api_key"] == "sk-test-key"

    def test_disabled_when_api_key_missing(self):
        """Transcription should be disabled when OPENAI_API_KEY is not set."""
        with mock.patch.dict(os.environ, {}, clear=True):
            config = {"transcribe": {}}
            result = get_transcribe_config(config)
            assert result["enabled"] is False
            assert result["api_key"] is None

    def test_explicit_enable_overrides_api_key_check(self):
        """Explicit enabled=True should be respected even without API key."""
        with mock.patch.dict(os.environ, {}, clear=True):
            config = {"transcribe": {"enabled": True}}
            result = get_transcribe_config(config)
            assert result["enabled"] is True

    def test_explicit_disable_works(self):
        """Explicit enabled=False should disable transcription."""
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            config = {"transcribe": {"enabled": False}}
            result = get_transcribe_config(config)
            assert result["enabled"] is False

    def test_defaults_are_correct(self):
        """Default configuration values should match spec."""
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            config = {"transcribe": {}}
            result = get_transcribe_config(config)
            assert result["provider"] == "openai"
            assert result["model"] == "whisper-1"
            assert result["language"] is None
            assert result["max_file_size"] == 25000000
            assert result["detect_non_speech"] is True
            assert result["fallback_to_attachment"] is True

    def test_custom_values_override_defaults(self):
        """Custom configuration should override defaults."""
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            config = {
                "transcribe": {
                    "model": "whisper-large",
                    "language": "en",
                    "max_file_size": 10000000,
                }
            }
            result = get_transcribe_config(config)
            assert result["model"] == "whisper-large"
            assert result["language"] == "en"
            assert result["max_file_size"] == 10000000

    def test_custom_api_key_env_name(self):
        """Should support custom API key environment variable name."""
        with mock.patch.dict(os.environ, {"MY_CUSTOM_KEY": "sk-custom"}):
            config = {
                "transcribe": {
                    "api_key_env": "MY_CUSTOM_KEY",
                }
            }
            result = get_transcribe_config(config)
            assert result["api_key"] == "sk-custom"


class TestIsSpeechContent:
    """Test speech content detection."""

    def test_empty_transcript_is_not_speech(self):
        """Empty or whitespace-only transcript should return False."""
        assert is_speech_content("") is False
        assert is_speech_content("   ") is False
        assert is_speech_content("\n\t") is False

    def test_music_marker_detected(self):
        """Transcripts with [MUSIC] marker should return False."""
        assert is_speech_content("[MUSIC]") is False
        assert is_speech_content("[MUSIC] some ambient sound") is False

    def test_noise_marker_detected(self):
        """Transcripts with [NOISE] marker should return False."""
        assert is_speech_content("[NOISE]") is False
        assert is_speech_content("[NOISE] background") is False

    def test_silence_marker_detected(self):
        """Transcripts with [SILENCE] marker should return False."""
        assert is_speech_content("[SILENCE]") is False

    def test_very_short_transcript_is_not_speech(self):
        """Very short transcripts (< 10 chars) are considered noise."""
        assert is_speech_content("hello") is False
        assert is_speech_content("123") is False
        assert is_speech_content("a") is False

    def test_normal_speech_detected(self):
        """Normal English speech should return True."""
        assert is_speech_content("This is a test recording") is True
        assert (
            is_speech_content("Welcome to the podcast, today we'll discuss AI and machine learning")
            is True
        )

    def test_speech_with_punctuation(self):
        """Speech with punctuation should still be detected."""
        assert is_speech_content("Hello, how are you? I'm doing well.") is True

    def test_mostly_symbols_not_detected(self):
        """Transcripts that are mostly symbols should return False."""
        assert is_speech_content("!@#$%^&*() !@#") is False
        # Note: dots are considered printable so they still pass ratio check,
        # but they're too short so they'll be caught by short length check
        assert is_speech_content("!@#$%^&*()") is False

    def test_case_insensitive_marker_detection(self):
        """Marker detection should be case-insensitive."""
        assert is_speech_content("[music]") is False
        assert is_speech_content("[Music]") is False
        assert is_speech_content("[NOISE]") is False
        assert is_speech_content("[noise]") is False


@pytest.mark.asyncio
class TestTranscribeAudio:
    """Test audio transcription."""

    async def test_transcription_disabled(self):
        """Should return None if transcription is disabled."""
        with mock.patch.dict(os.environ, {}, clear=True):
            config = {"transcribe": {"enabled": False}}
            result = await transcribe_audio(b"fake audio data", "audio/mp3", config)
            assert result is None

    async def test_no_api_key(self):
        """Should return None if API key is not available."""
        with mock.patch.dict(os.environ, {}, clear=True):
            config = {"transcribe": {}}
            result = await transcribe_audio(b"fake audio data", "audio/mp3", config)
            assert result is None

    async def test_file_too_large(self):
        """Should return None if file exceeds size limit."""
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            config = {
                "transcribe": {
                    "max_file_size": 1000,  # Very small limit
                }
            }
            # Create 2000 byte "audio" data
            large_data = b"x" * 2000
            result = await transcribe_audio(large_data, "audio/mp3", config)
            assert result is None

    async def test_unsupported_provider(self):
        """Should return None if provider is not 'openai'."""
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            config = {
                "transcribe": {
                    "provider": "google",  # Not supported
                }
            }
            result = await transcribe_audio(b"fake audio data", "audio/mp3", config)
            assert result is None

    @mock.patch("gptsh.core.transcribe.httpx.AsyncClient")
    async def test_successful_transcription(self, mock_client):
        """Should return transcript on successful API call."""
        # Mock the API response
        mock_response = mock.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"text": "Hello, this is a test recording."}

        mock_async_client = mock.MagicMock()
        mock_async_client.post = mock.AsyncMock(return_value=mock_response)
        mock_async_client.__aenter__ = mock.AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = mock.AsyncMock(return_value=None)

        mock_client.return_value = mock_async_client

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            config = {"transcribe": {}, "providers": {"openai": {}}}
            result = await transcribe_audio(b"fake audio data", "audio/mp3", config)
            assert result == "Hello, this is a test recording."

    @mock.patch("gptsh.core.transcribe.httpx.AsyncClient")
    async def test_api_error_returns_none(self, mock_client):
        """Should return None on API error."""
        mock_response = mock.MagicMock()
        mock_response.status_code = 500

        mock_async_client = mock.MagicMock()
        mock_async_client.post = mock.AsyncMock(return_value=mock_response)
        mock_async_client.__aenter__ = mock.AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = mock.AsyncMock(return_value=None)

        mock_client.return_value = mock_async_client

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            config = {"transcribe": {}, "providers": {"openai": {}}}
            result = await transcribe_audio(b"fake audio data", "audio/mp3", config)
            assert result is None

    @mock.patch("gptsh.core.transcribe.httpx.AsyncClient")
    async def test_non_speech_content_returns_none(self, mock_client):
        """Should return None if transcript contains only non-speech markers."""
        mock_response = mock.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"text": "[MUSIC]"}

        mock_async_client = mock.MagicMock()
        mock_async_client.post = mock.AsyncMock(return_value=mock_response)
        mock_async_client.__aenter__ = mock.AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = mock.AsyncMock(return_value=None)

        mock_client.return_value = mock_async_client

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            config = {
                "transcribe": {
                    "detect_non_speech": True,
                }
            }
            result = await transcribe_audio(b"fake audio data", "audio/mp3", config)
            # Should be None because non-speech detected
            assert result is None

    @mock.patch("gptsh.core.transcribe.httpx.AsyncClient")
    async def test_non_speech_detection_disabled(self, mock_client):
        """Should return transcript even for non-speech if detection is disabled."""
        mock_response = mock.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"text": "[MUSIC]"}

        mock_async_client = mock.MagicMock()
        mock_async_client.post = mock.AsyncMock(return_value=mock_response)
        mock_async_client.__aenter__ = mock.AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = mock.AsyncMock(return_value=None)

        mock_client.return_value = mock_async_client

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            config = {
                "transcribe": {
                    "detect_non_speech": False,
                }
            }
            result = await transcribe_audio(b"fake audio data", "audio/mp3", config)
            # Should return transcript even though it's [MUSIC]
            assert result == "[MUSIC]"

    @mock.patch("gptsh.core.transcribe.httpx.AsyncClient")
    async def test_language_hint_in_request(self, mock_client):
        """Should include language hint in API request if configured."""
        mock_response = mock.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"text": "Bonjour, comment allez-vous aujourd'hui?"}

        mock_async_client = mock.MagicMock()
        mock_async_client.post = mock.AsyncMock(return_value=mock_response)
        mock_async_client.__aenter__ = mock.AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = mock.AsyncMock(return_value=None)

        mock_client.return_value = mock_async_client

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            config = {
                "transcribe": {
                    "language": "fr",
                }
            }
            result = await transcribe_audio(b"fake audio data", "audio/mp3", config)
            assert result == "Bonjour, comment allez-vous aujourd'hui?"
            # Verify language was passed in params
            call_kwargs = mock_async_client.post.call_args.kwargs
            assert call_kwargs["params"]["language"] == "fr"
