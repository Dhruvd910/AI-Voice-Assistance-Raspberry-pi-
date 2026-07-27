import assist


def test_transcribe_with_groq_uses_prompt_and_returns_text(monkeypatch):
    calls = []

    class FakeTranscriptions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return "  hello groq  "

    class FakeAudio:
        transcriptions = FakeTranscriptions()

    class FakeGroqClient:
        audio = FakeAudio()

    monkeypatch.setattr(assist, "groq_client", FakeGroqClient())

    result = assist.transcribe_with_groq(b"wav-bytes", "context prompt")

    assert result == "hello groq"
    assert calls == [
        {
            "file": ("temp.wav", b"wav-bytes"),
            "model": "whisper-large-v3-turbo",
            "response_format": "text",
            "language": "en",
            "temperature": 0.0,
            "prompt": "context prompt",
        }
    ]
