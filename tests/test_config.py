from pathlib import Path
from busybar.config import load_config

def test_defaults_when_no_file(tmp_path):
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg["device"]["host"] == "10.0.4.20"
    assert cfg["calendar_countdown"]["poll_seconds"] == 60
    assert cfg["ci_status"]["repos"] == []

def test_file_overrides_defaults(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[device]\nhost = "192.0.2.7"\n[ci_status]\nrepos = ["a/b"]\n')
    cfg = load_config(p)
    assert cfg["device"]["host"] == "192.0.2.7"
    assert cfg["ci_status"]["repos"] == ["a/b"]
    assert cfg["calendar_countdown"]["warn_minutes"] == 5  # untouched default

def test_env_overrides_file(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text('[device]\nhost = "192.0.2.7"\n')
    monkeypatch.setenv("BUSYBAR_HOST", "192.0.2.99")
    assert load_config(p)["device"]["host"] == "192.0.2.99"

def test_returned_config_mutation_does_not_corrupt_defaults(tmp_path):
    # Mutate the returned config in place
    cfg1 = load_config(tmp_path / "missing.toml")
    cfg1["ci_status"]["repos"].append("mutated/repo")
    cfg1["calendar_countdown"]["poll_seconds"] = 999

    # Load a fresh config and verify it is unaffected
    cfg2 = load_config(tmp_path / "missing.toml")
    assert cfg2["ci_status"]["repos"] == []
    assert cfg2["calendar_countdown"]["poll_seconds"] == 60
