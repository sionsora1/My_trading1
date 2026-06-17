import importlib


def test_api_key_can_be_loaded_from_environment(monkeypatch):
    monkeypatch.setenv('QUANT_API_KEY', 'test-secret')

    import config.settings as settings

    reloaded = importlib.reload(settings)
    assert reloaded.API_KEY == 'test-secret'
