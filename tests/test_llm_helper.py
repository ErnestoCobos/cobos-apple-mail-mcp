from __future__ import annotations

import stat
import sys

import pytest

from cobos_apple_mail_mcp.read.llm_helper import find_binary, is_available, summarize


def test_unavailable_when_no_binary_found(monkeypatch):
    monkeypatch.delenv("APPLE_MAIL_LLM_HELPER", raising=False)
    monkeypatch.setattr("shutil.which", lambda _name: None)
    assert find_binary() is None
    assert is_available() is False


def test_summarize_raises_clear_error_when_unbuilt(monkeypatch):
    monkeypatch.delenv("APPLE_MAIL_LLM_HELPER", raising=False)
    monkeypatch.setattr("shutil.which", lambda _name: None)
    with pytest.raises(RuntimeError, match="not built"):
        summarize("some text")


def _make_fake_binary(tmp_path, script: str):
    path = tmp_path / "fake-helper"
    path.write_text(f"#!{sys.executable}\n{script}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def test_summarize_returns_summary_from_env_binary(tmp_path, monkeypatch):
    script = (
        "import sys, json\n"
        "json.loads(sys.stdin.read())\n"
        "print(json.dumps({'summary': 'a short summary'}))\n"
    )
    binary = _make_fake_binary(tmp_path, script)
    monkeypatch.setenv("APPLE_MAIL_LLM_HELPER", str(binary))

    assert find_binary() == binary
    assert is_available() is True
    assert summarize("long email text") == "a short summary"


def test_summarize_surfaces_helper_error(tmp_path, monkeypatch):
    script = "import json; print(json.dumps({'error': 'model declined'}))\n"
    binary = _make_fake_binary(tmp_path, script)
    monkeypatch.setenv("APPLE_MAIL_LLM_HELPER", str(binary))

    with pytest.raises(RuntimeError, match="model declined"):
        summarize("text")


def test_summarize_times_out_on_hung_helper(tmp_path, monkeypatch):
    script = "import time; time.sleep(30)\n"
    binary = _make_fake_binary(tmp_path, script)
    monkeypatch.setenv("APPLE_MAIL_LLM_HELPER", str(binary))

    with pytest.raises(RuntimeError, match="exceeded"):
        summarize("text", timeout_sec=0.5)
