from pathlib import Path

from tailhedge.paths import default_runs_dir


def test_default_runs_dir_is_cwd_runs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert default_runs_dir() == tmp_path / "runs"


def test_default_runs_dir_does_not_create_the_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    default_runs_dir()
    assert not (tmp_path / "runs").exists()
