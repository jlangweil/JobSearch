"""config.yaml is personal and git-ignored: a fresh checkout starts from the committed config.example.yaml."""

from __future__ import annotations

import shutil

from jobhunt.config import PROJECT_ROOT, load_config


def test_first_run_copies_the_template(tmp_path):
    shutil.copyfile(PROJECT_ROOT / "config.example.yaml", tmp_path / "config.example.yaml")
    cfg = load_config(tmp_path / "config.yaml")
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == \
        (tmp_path / "config.example.yaml").read_text(encoding="utf-8")
    assert cfg.config_path == (tmp_path / "config.yaml").resolve()
    # a fresh install starts safe: no auto-apply, localhost only, and site passwords encrypted
    assert not cfg.auto_apply.enabled and cfg.web.host == "127.0.0.1"
    assert cfg.auto_apply.store_passwords == "encrypted"
    assert "projects" in cfg.tailoring.sections


def test_an_existing_config_is_left_alone(tmp_path):
    shutil.copyfile(PROJECT_ROOT / "config.example.yaml", tmp_path / "config.example.yaml")
    (tmp_path / "config.yaml").write_text("profile:\n  headline: mine\n", encoding="utf-8")
    assert load_config(tmp_path / "config.yaml").profile.headline == "mine"
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == "profile:\n  headline: mine\n"
