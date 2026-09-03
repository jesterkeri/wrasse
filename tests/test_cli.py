from __future__ import annotations

import json

from wrasse.cli import main


PROVIDER = "0x3333333333333333333333333333333333333333"


def test_cold_start_policy_is_emitted_as_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WRASSE_MEMORY_PATH", str(tmp_path / "memory.db"))
    output_path = tmp_path / "policy.json"
    assert main(["policy", PROVIDER, "--output", str(output_path)]) == 0
    output = json.loads(capsys.readouterr().out)
    written = json.loads(output_path.read_text())
    assert output == written
    assert output["cold_start"] is True
    assert set(output["profiles"]) == {"urgent", "budget", "sensitive"}
    assert all(value["policy_hash"].startswith("0x") for value in output["profiles"].values())
