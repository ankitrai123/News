

def test_dotenv_accepts_notepad_variants(tmp_path, monkeypatch):
    """Windows Notepad saves `.env` as `.env.txt`, sometimes as UTF-16 or with a BOM."""
    import os
    from intelpod import config
    monkeypatch.delenv("INTELPOD_NO_DOTENV", raising=False)
    monkeypatch.delenv("X_TEST_KEY", raising=False)
    monkeypatch.delenv("X_OTHER", raising=False)
    (tmp_path / ".env.txt").write_bytes('X_TEST_KEY="abc"\n  X_OTHER = 1\n'.encode("utf-16"))
    config._load_dotenv(tmp_path / ".env")
    assert os.environ["X_TEST_KEY"] == "abc" and os.environ["X_OTHER"] == "1"
    monkeypatch.delenv("X_TEST_KEY")
    (tmp_path / ".env").write_bytes('﻿X_TEST_KEY=xyz\n'.encode("utf-8"))
    config._load_dotenv(tmp_path / ".env")
    assert os.environ["X_TEST_KEY"] == "xyz"
