"""Task 5: review timer and the review-effort reduction calculation."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))
from eval import run_eval  # noqa: E402
from scripts import review_timer as rt  # noqa: E402


@pytest.fixture
def f(tmp_path, monkeypatch):
    p = tmp_path / "re.json"
    monkeypatch.setenv("REVIEW_EFFORT_FILE", str(p))
    return p


def test_timer_cycle(f, capsys):
    rt.main(["start", "manual"])
    with pytest.raises(SystemExit):
        rt.main(["start", "manual"])  # already running
    rt.main(["note", "looked", "at", "ports", "--phase", "manual"])
    rt.main(["stop", "manual"])
    rt.main(["found", "manual", "5", "--ids", "M1,C3"])
    d = json.loads(f.read_text())
    assert d["phases"]["manual"]["found"] == 5 and d["phases"]["manual"]["found_ids"] == ["M1", "C3"]
    assert d["notes"][0]["text"] == "looked at ports" and d["phases"]["manual"]["intervals"][0]["stop"]
    with pytest.raises(SystemExit):
        rt.main(["stop", "manual"])
    with pytest.raises(SystemExit):
        rt.main(["start", "bogus"])
    rt.main(["status"])
    assert "manual:" in capsys.readouterr().out


def test_minutes_sums_closed_intervals():
    d = rt.fresh()
    rt.start(d, "tool", "2026-01-01T10:00:00+00:00")
    rt.stop(d, "tool", "2026-01-01T10:30:00+00:00")
    rt.start(d, "tool", "2026-01-01T11:00:00+00:00")  # still running: not counted
    assert rt.minutes(d["phases"]["tool"]) == 30.0


def test_reduction_calculation(tmp_path):
    d = rt.fresh()
    rt.start(d, "manual", "2026-01-01T10:00:00+00:00")
    rt.stop(d, "manual", "2026-01-01T11:00:00+00:00")
    rt.start(d, "tool", "2026-01-01T12:00:00+00:00")
    rt.stop(d, "tool", "2026-01-01T12:15:00+00:00")
    d["phases"]["manual"]["found"], d["phases"]["tool"]["found"] = 4, 7
    p = tmp_path / "x.json"
    rt.save(d, p)
    r = run_eval.review_effort_summary(p)
    assert r == {"status": "measured", "manual_minutes": 60.0, "tool_minutes": 15.0, "reduction_pct": 75.0,
                 "manual_found": 4, "tool_found": 7}
    assert "75.0% reduction" in "\n".join(run_eval._review_effort_md(r))


def test_not_measured(tmp_path):
    assert run_eval.review_effort_summary(tmp_path / "missing.json")["status"] == "not measured"
    p = tmp_path / "half.json"
    rt.save(rt.fresh(), p)
    assert run_eval.review_effort_summary(p)["status"] == "not measured"
    assert "Not measured" in "\n".join(run_eval._review_effort_md({"status": "not measured"}))
