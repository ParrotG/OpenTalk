"""Make paid network tests opt-in, even when credentials are present."""

import pytest


@pytest.fixture(autouse=True)
def isolated_session_storage(tmp_path, monkeypatch):
    """Keep session and telemetry writes away from the demo databases."""
    from dataclasses import replace
    from opentalk.sessions import config
    configuration = replace(config.load_session_config(), database_path=tmp_path / "sessions.sqlite3",
                            telemetry_database_path=tmp_path / "telemetry.sqlite3")
    monkeypatch.setattr(config, "load_session_config", lambda path=None: configuration)
    from opentalk.voice import session, text
    monkeypatch.setattr(session, "load_session_config", lambda path=None: configuration)
    monkeypatch.setattr(text, "load_session_config", lambda path=None: configuration)


def pytest_addoption(parser):
    parser.addoption("--live-llm", action="store_true", default=False,
                     help="Run paid DeepSeek integration tests using configured credentials.")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--live-llm"):
        for item in items:
            if "live_llm" in item.keywords:
                item.add_marker(pytest.mark.skip(reason="Use --live-llm to enable paid API tests."))
