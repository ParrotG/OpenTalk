"""Exercise configuration and the executable entry point in isolated databases."""

import json
import os
import subprocess
import sys

import pytest

from opentalk.config import PROJECT_ROOT, load_config


def test_cli_smoke_twice(tmp_path):
    command = [sys.executable, "-m", "opentalk", "--database",
               str(tmp_path / "cli.sqlite3"), "smoke"]
    environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "backend")}
    results = []
    for _ in range(2):
        process = subprocess.run(command, env=environment, cwd=tmp_path,
                                 capture_output=True, text=True, check=True)
        result = json.loads(process.stdout)
        assert result["status"] == "passed"
        assert result["cancellation"]["status"] == "cancelled"
        results.append(result)
    assert results[0]["booking"]["booking_id"] != results[1]["booking"]["booking_id"]


def test_config_and_missing_configuration(tmp_path):
    config = load_config()
    assert config.database_path == PROJECT_ROOT / "data/opentalk.sqlite3"
    assert config.timezone == "Asia/Singapore"
    with pytest.raises(ValueError, match="Invalid backend configuration"):
        load_config(tmp_path / "missing.toml")
    invalid = tmp_path / "invalid.toml"
    text = (PROJECT_ROOT / "config/backend.toml").read_text(encoding="utf-8")
    invalid.write_text(text.replace("[9, 10, 14, 15]", "[9, 9]"), encoding="utf-8")
    with pytest.raises(ValueError, match="Slot start hours"):
        load_config(invalid)
