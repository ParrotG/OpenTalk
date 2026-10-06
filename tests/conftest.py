"""Make paid network tests opt-in, even when credentials are present."""

import pytest


def pytest_addoption(parser):
    parser.addoption("--live-llm", action="store_true", default=False,
                     help="Run paid DeepSeek integration tests using configured credentials.")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--live-llm"):
        for item in items:
            if "live_llm" in item.keywords:
                item.add_marker(pytest.mark.skip(reason="Use --live-llm to enable paid API tests."))
