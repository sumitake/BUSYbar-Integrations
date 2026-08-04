import tomllib
from pathlib import Path
from busybar.config import DEFAULTS, load_config

REPO_ROOT = Path(__file__).resolve().parents[1]

def test_defaults_when_no_file(tmp_path):
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg["device"]["host"] == "10.0.4.20"
    # v1.6 cloud transport fallback -- disabled out of the box.
    assert cfg["device"]["cloud_token"] == ""
    assert cfg["device"]["cloud_base_url"] == "https://api.busy.app/busybar"
    assert cfg["device"]["transport"] == "auto"
    assert cfg["calendar_countdown"]["poll_seconds"] == 10  # v1.5 ambient-tier default
    assert cfg["ci_status"]["repos"] == []
    assert cfg["ci_status"]["show_running"] is True
    assert cfg["ci_status"]["running_poll_seconds"] == 20
    assert cfg["ci_status"]["show_quota"] is True
    # v1.5.1 account-wide watching defaults
    assert cfg["ci_status"]["watch_account_repos"] is False
    assert cfg["ci_status"]["repos_exclude"] == []
    assert cfg["ci_status"]["active_within_days"] == 30
    assert cfg["ci_status"]["repo_refresh_minutes"] == 60

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
    assert cfg2["calendar_countdown"]["poll_seconds"] == 10


# --- config.example.toml parity (v1.6) --------------------------------------
# config.example.toml's own header claims "All keys optional; defaults
# shown" -- these tests hold that claim to account for the [device] table
# specifically, since it's the one this round touches and the one where a
# drifted example (e.g. a non-empty placeholder token) would be a real
# hygiene problem, not just documentation staleness.

def test_example_toml_device_section_matches_defaults():
    with open(REPO_ROOT / "config.example.toml", "rb") as fh:
        example = tomllib.load(fh)
    assert example["device"] == DEFAULTS["device"]

def test_example_toml_cloud_token_is_empty_placeholder_not_a_real_looking_token():
    with open(REPO_ROOT / "config.example.toml", "rb") as fh:
        example = tomllib.load(fh)
    # Guards against ever accidentally shipping a real-looking credential
    # in the committed example file -- the real token belongs only in the
    # git-ignored config.toml.
    assert example["device"]["cloud_token"] == ""
