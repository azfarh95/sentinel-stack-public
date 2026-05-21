"""Unit tests for fx.save_fx / get_fx YAML manipulation."""
from app import fx as fx_mod


def test_get_fx_defaults_when_missing(tmp_path, monkeypatch):
    fake = tmp_path / "config.yaml"
    fake.write_text("usd_to_sgd: 1.27\n")
    monkeypatch.setattr(fx_mod, "CONFIG_PATH", fake)
    out = fx_mod.get_fx()
    assert out["rate"] == 1.27
    assert out["source"] == "manual"   # default when fx_source missing


def test_save_fx_writes_all_three_keys(tmp_path, monkeypatch):
    fake = tmp_path / "config.yaml"
    fake.write_text(
        "# header comment\n"
        "usd_to_sgd: 1.30\n"
        "fx_source: \"xe.com\"\n"
        "fx_last_updated: \"2026-01-01\"\n"
        "other_key: keep_me\n"
    )
    monkeypatch.setattr(fx_mod, "CONFIG_PATH", fake)
    fx_mod.save_fx(1.27, "manual")
    text = fake.read_text()
    assert "usd_to_sgd: 1.27" in text
    assert 'fx_source: "manual"' in text
    assert "other_key: keep_me" in text     # other keys preserved
    # last_updated is today
    state = fx_mod.get_fx()
    assert state["rate"] == 1.27
    assert state["source"] == "manual"


def test_save_fx_adds_missing_fx_source(tmp_path, monkeypatch):
    """If yaml lacks fx_source key, save should append it."""
    fake = tmp_path / "config.yaml"
    fake.write_text("usd_to_sgd: 1.30\n")
    monkeypatch.setattr(fx_mod, "CONFIG_PATH", fake)
    fx_mod.save_fx(1.27, "xe.com")
    text = fake.read_text()
    assert 'fx_source: "xe.com"' in text
    assert "fx_last_updated:" in text


def test_sources_constant_has_three_options():
    assert "manual" in fx_mod.SOURCES
    assert "xe.com" in fx_mod.SOURCES
    assert "oanda" in fx_mod.SOURCES
