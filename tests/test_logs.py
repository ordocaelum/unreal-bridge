import os
import re
import time

import pytest

from unreal_bridge import logs


def test_tail_last_lines(tmp_path):
    p = tmp_path / "Game.log"
    p.write_text("\ufeff" + "".join(f"line {i}\r\n" for i in range(10)), encoding="utf-8")
    res = logs.tail(p, 3)
    assert res["lines"] == ["line 7", "line 8", "line 9"]
    assert logs.tail(p, 100)["lines"][0] == "line 0"


def test_tail_pattern_across_blocks(tmp_path):
    p = tmp_path / "Big.log"
    with p.open("w", encoding="utf-8") as fh:
        for i in range(40000):
            fh.write(f"LogTemp: filler {i} {'x' * 20}\n")
            if i % 5000 == 0:
                fh.write(f"LogNet: Error: dropped {i}\n")
    res = logs.tail(p, 3, pattern=r"lognet: error")
    assert res["lines"] == ["LogNet: Error: dropped 25000", "LogNet: Error: dropped 30000", "LogNet: Error: dropped 35000"]
    lines = logs.tail(p, 5000)["lines"]
    assert len(lines) == 5000
    assert all(re.fullmatch(r"LogTemp: filler \d+ x{20}|LogNet: Error: dropped \d+", line) for line in lines)


def test_pick_newest_and_named(tmp_path):
    d = tmp_path / "Saved" / "Logs"
    d.mkdir(parents=True)
    old = d / "Backrooms-backup-2026.10.01-10.00.00.log"
    new = d / "Backrooms.log"
    old.write_text("old\n")
    new.write_text("new\n")
    past = time.time() - 100
    os.utime(old, (past, past))
    assert logs.pick_log(d) == new
    assert logs.pick_log(d, "Backrooms-backup-2026.10.01-10.00.00") == old
    with pytest.raises(FileNotFoundError):
        logs.pick_log(d, "nope.log")
    with pytest.raises(FileNotFoundError):
        logs.pick_log(tmp_path)


def test_logs_dir_accepts_uproject(tmp_path):
    assert logs.logs_dir(tmp_path / "Backrooms.uproject") == tmp_path / "Saved" / "Logs"
    assert logs.logs_dir(tmp_path) == tmp_path / "Saved" / "Logs"
