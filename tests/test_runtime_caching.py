import assist


def test_cached_whisper_model_is_reused(monkeypatch):
    calls = []

    class FakeWhisperModel:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(assist, "WhisperModel", FakeWhisperModel)
    assist.WHISPER_MODEL = None

    model_a = assist.get_cached_whisper_model()
    model_b = assist.get_cached_whisper_model()

    assert model_a is model_b
    assert len(calls) == 1
