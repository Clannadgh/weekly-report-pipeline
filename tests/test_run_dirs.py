from pathlib import Path

from pipeline import agent, s1_intake


def test_choose_run_dir_fresh_starts_new_folder(tmp_path):
    (tmp_path / "w44").mkdir()
    (tmp_path / "w44" / "status.json").write_text('{"state": "准入通过"}', encoding="utf-8")

    assert s1_intake._choose_run_dir(tmp_path, "w44").name == "w44"
    assert s1_intake._choose_run_dir(tmp_path, "w44", fresh=True).name == "w44-2"

    (tmp_path / "w44-2").mkdir()
    assert s1_intake._choose_run_dir(tmp_path, "w44", fresh=True).name == "w44-3"


def test_resolve_run_dir_stays_inside_root(tmp_path):
    (tmp_path / "w44-2").mkdir()
    assert s1_intake._resolve_run_dir(tmp_path, "w44-2") == (tmp_path / "w44-2").resolve()
    assert s1_intake._resolve_run_dir(tmp_path, "runs/w44-2") == (tmp_path / "w44-2").resolve()
    assert s1_intake._resolve_run_dir(tmp_path, "../w44-2") is None


def test_each_session_creates_its_own_run_folder(tmp_path, monkeypatch):
    (tmp_path / "w44").mkdir()
    (tmp_path / "w44" / "status.json").write_text('{"state": "准入通过"}', encoding="utf-8")
    calls = []

    def fake_run_intake(files, prompt, run_root, spec_path=None, use_ai=None,
                        progress=None, fresh_run=False, reuse_run_dir=None):
        calls.append({"fresh": fresh_run, "reuse": reuse_run_dir})
        if reuse_run_dir:
            name = str(reuse_run_dir)
        else:
            name = s1_intake._choose_run_dir(
                Path(run_root), "w44", fresh=fresh_run).name
        (Path(run_root) / name).mkdir(parents=True, exist_ok=True)

        class Result:
            def model_dump(self):
                return {"run": {"period_tag": name, "dir": "runs/" + name},
                        "state": "准入通过"}

        return Result(), None, []

    monkeypatch.setattr(agent, "run_intake", fake_run_intake)
    minutes = tmp_path / "minutes.md"
    minutes.write_text("纪要", encoding="utf-8")

    first = agent.Session([minutes], run_root=tmp_path)
    first._run_s1({}, "开始")
    assert first.last_run == "w44-2"

    second = agent.Session([minutes], run_root=tmp_path)
    second._run_s1({}, "开始")
    assert second.last_run == "w44-3"

    first._run_s1({}, "继续")
    assert first.last_run == "w44-2"

    assert calls == [
        {"fresh": True, "reuse": None},
        {"fresh": True, "reuse": None},
        {"fresh": False, "reuse": "w44-2"},
    ]


def test_delivered_run_is_not_reused(tmp_path, monkeypatch):
    folder = tmp_path / "w44"
    folder.mkdir()
    (folder / "status.json").write_text('{"state": "已交付"}', encoding="utf-8")
    calls = []

    def fake_run_intake(files, prompt, run_root, spec_path=None, use_ai=None,
                        progress=None, fresh_run=False, reuse_run_dir=None):
        calls.append({"fresh": fresh_run, "reuse": reuse_run_dir})

        class Result:
            def model_dump(self):
                return {"run": {"period_tag": "w44-2", "dir": "runs/w44-2"},
                        "state": "准入通过"}

        return Result(), None, []

    monkeypatch.setattr(agent, "run_intake", fake_run_intake)
    minutes = tmp_path / "minutes.md"
    minutes.write_text("纪要", encoding="utf-8")
    session = agent.Session([minutes], run_root=tmp_path)
    session.last_run = "w44"

    session._run_s1({}, "重新检查")

    assert calls == [{"fresh": True, "reuse": None}]
    assert session.last_run == "w44-2"
