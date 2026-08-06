import base64
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api import server


class _FakeTranscriptions:
    def __init__(self, text: str = "hello world", error: Exception | None = None):
        self.calls = []
        self.text = text
        self.error = error

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(text=self.text)


def _fake_client(**kwargs):
    client = SimpleNamespace()
    client.audio = SimpleNamespace(transcriptions=_FakeTranscriptions(**kwargs))
    return client


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_transcribe_success_maps_mime_to_filename(monkeypatch) -> None:
    fake = _fake_client()
    monkeypatch.setattr(server, "_openai_audio_client", lambda: fake)
    client = TestClient(server.app)
    response = client.post(
        "/transcribe",
        json={"audio_b64": _b64(b"fakeaudio"), "mime_type": "audio/webm;codecs=opus"},
    )
    assert response.status_code == 200
    assert response.json() == {"text": "hello world"}
    call = fake.audio.transcriptions.calls[0]
    # Codec suffix stripped, filename hint chosen for the container.
    assert call["file"][0] == "audio.webm"
    assert call["file"][1] == b"fakeaudio"


def test_transcribe_safari_mp4_filename(monkeypatch) -> None:
    fake = _fake_client()
    monkeypatch.setattr(server, "_openai_audio_client", lambda: fake)
    client = TestClient(server.app)
    response = client.post(
        "/transcribe", json={"audio_b64": _b64(b"aac"), "mime_type": "audio/mp4"}
    )
    assert response.status_code == 200
    assert fake.audio.transcriptions.calls[0]["file"][0] == "audio.mp4"


def test_transcribe_model_env_override(monkeypatch) -> None:
    fake = _fake_client()
    monkeypatch.setattr(server, "_openai_audio_client", lambda: fake)
    monkeypatch.setenv("TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")
    client = TestClient(server.app)
    client.post("/transcribe", json={"audio_b64": _b64(b"x")})
    assert fake.audio.transcriptions.calls[0]["model"] == "gpt-4o-mini-transcribe"


def test_transcribe_no_key_503(monkeypatch) -> None:
    monkeypatch.setattr(server, "_openai_audio_client", lambda: None)
    client = TestClient(server.app)
    response = client.post("/transcribe", json={"audio_b64": _b64(b"x")})
    assert response.status_code == 503
    assert "OPENAI_API_KEY" in response.json()["detail"]


def test_transcribe_factory_returns_none_without_env_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert server._openai_audio_client() is None


def test_transcribe_oversized_413(monkeypatch) -> None:
    monkeypatch.setattr(server, "_TRANSCRIBE_MAX_BYTES", 16)
    monkeypatch.setattr(server, "_openai_audio_client", _fake_client)
    client = TestClient(server.app)
    response = client.post("/transcribe", json={"audio_b64": _b64(b"x" * 17)})
    assert response.status_code == 413


def test_transcribe_bad_base64_and_empty_422(monkeypatch) -> None:
    monkeypatch.setattr(server, "_openai_audio_client", _fake_client)
    client = TestClient(server.app)
    assert client.post(
        "/transcribe", json={"audio_b64": "not@@base64!!"}
    ).status_code == 422
    assert client.post("/transcribe", json={"audio_b64": ""}).status_code == 422


def test_transcribe_provider_error_502(monkeypatch) -> None:
    fake = _fake_client(error=RuntimeError("boom"))
    monkeypatch.setattr(server, "_openai_audio_client", lambda: fake)
    client = TestClient(server.app)
    response = client.post("/transcribe", json={"audio_b64": _b64(b"x")})
    assert response.status_code == 502
    assert "Transcription failed" in response.json()["detail"]


# ── /speak (natural interviewer voice) ───────────────────────────────

class _FakeSpeech:
    def __init__(self, error: Exception | None = None):
        self.calls = []
        self.error = error

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(content=b"mp3bytes")


def _fake_speech_client(**kwargs):
    client = SimpleNamespace()
    client.audio = SimpleNamespace(speech=_FakeSpeech(**kwargs))
    return client


def test_speak_returns_audio(monkeypatch) -> None:
    fake = _fake_speech_client()
    monkeypatch.setattr(server, "_openai_audio_client", lambda: fake)
    client = TestClient(server.app)
    response = client.post("/speak", json={"text": "Explain Kafka rebalancing."})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/mpeg")
    assert response.content == b"mp3bytes"
    call = fake.audio.speech.calls[0]
    assert call["input"] == "Explain Kafka rebalancing."
    assert call["voice"]  # default voice applied


def test_speak_voice_and_model_env_override(monkeypatch) -> None:
    fake = _fake_speech_client()
    monkeypatch.setattr(server, "_openai_audio_client", lambda: fake)
    monkeypatch.setenv("TTS_MODEL", "tts-1-hd")
    monkeypatch.setenv("TTS_VOICE", "onyx")
    client = TestClient(server.app)
    client.post("/speak", json={"text": "hi there"})
    call = fake.audio.speech.calls[0]
    assert call["model"] == "tts-1-hd"
    assert call["voice"] == "onyx"


def test_speak_no_key_503_empty_422_long_413(monkeypatch) -> None:
    monkeypatch.setattr(server, "_openai_audio_client", lambda: None)
    client = TestClient(server.app)
    assert client.post("/speak", json={"text": "hello"}).status_code == 503
    monkeypatch.setattr(server, "_openai_audio_client", _fake_speech_client)
    assert client.post("/speak", json={"text": "   "}).status_code == 422
    assert client.post("/speak", json={"text": "x" * 2001}).status_code == 413


def test_speak_provider_error_502(monkeypatch) -> None:
    fake = _fake_speech_client(error=RuntimeError("no such voice"))
    monkeypatch.setattr(server, "_openai_audio_client", lambda: fake)
    client = TestClient(server.app)
    response = client.post("/speak", json={"text": "hello"})
    assert response.status_code == 502
