"""env_store must upsert keys in place and preserve everything else -- this
is what the Settings page's POST /api/settings relies on to edit .env safely."""
import os
import tempfile

import pytest

from env_store import read_env_file, update_env_file


@pytest.fixture()
def temp_env_path():
    fd, path = tempfile.mkstemp(suffix=".env")
    os.close(fd)
    yield path
    os.remove(path)


def _write(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def test_updates_existing_key_in_place(temp_env_path):
    _write(temp_env_path, "# comment\nLLM_PROVIDER=ollama\nDRY_RUN=true\n")
    update_env_file({"LLM_PROVIDER": "gemini"}, path=temp_env_path)

    with open(temp_env_path) as f:
        content = f.read()

    assert "LLM_PROVIDER=gemini" in content
    assert "DRY_RUN=true" in content
    assert "# comment" in content
    # order preserved -- LLM_PROVIDER line still comes before DRY_RUN
    assert content.index("LLM_PROVIDER") < content.index("DRY_RUN")


def test_appends_new_key(temp_env_path):
    _write(temp_env_path, "LLM_PROVIDER=ollama\n")
    update_env_file({"GEMINI_API_KEY": "abc123"}, path=temp_env_path)

    values = read_env_file(temp_env_path)
    assert values["GEMINI_API_KEY"] == "abc123"
    assert values["LLM_PROVIDER"] == "ollama"


def test_read_env_file_ignores_comments_and_blank_lines(temp_env_path):
    _write(temp_env_path, "# top comment\n\nKEY_A=1\n  \nKEY_B=2\n")
    values = read_env_file(temp_env_path)
    assert values == {"KEY_A": "1", "KEY_B": "2"}


def test_update_on_missing_file_creates_it(temp_env_path):
    os.remove(temp_env_path)
    update_env_file({"LLM_PROVIDER": "gemini"}, path=temp_env_path)
    assert read_env_file(temp_env_path) == {"LLM_PROVIDER": "gemini"}
