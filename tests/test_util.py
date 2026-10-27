import json

from unreal_bridge.util import fit


def test_small_values_untouched():
    obj = {"a": [1, 2, 3]}
    assert fit(obj, 1000) is obj


def test_lists_are_halved_until_they_fit():
    obj = {"count": 1000, "assets": [{"path": f"/Game/A_{i}"} for i in range(1000)]}
    out = fit(obj, 3000)
    assert len(json.dumps(out)) <= 3000
    assert out["count"] == 1000
    assert 0 < len(out["assets"]) < 1000
    assert out["_truncated"].startswith(f"assets: kept {len(out['assets'])} of 1000 items")
    assert out["assets"][0] == {"path": "/Game/A_0"}  # keeps the head of lists


def test_stdout_keeps_the_tail():
    out = fit({"stdout": "a" * 10000 + "END"}, 1500)
    assert out["stdout"].endswith("END")
    assert "stdout: kept" in out["_truncated"]


def test_non_dict_and_hopeless_cases():
    out = fit(list(range(5000)), 1000)
    assert "_truncated" in out and len(out["value"]) < 5000
    out = fit({"k" * 5000: 1}, 500)
    assert "preview" in out and len(out["preview"]) == 500
