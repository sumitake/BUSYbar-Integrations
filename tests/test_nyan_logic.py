from datetime import datetime
import pytest
from integrations.nyan_filler.logic import (
    parse_quiet_hours, in_quiet_hours, build_filler_elements, ELEMENT_ID)

def _at(h, m=0): return datetime(2026, 8, 6, h, m)

def test_parse_basic_and_empty():
    assert parse_quiet_hours("00:00-07:00") == (0, 420)
    assert parse_quiet_hours("23:00-07:00") == (1380, 420)
    assert parse_quiet_hours("") is None

@pytest.mark.parametrize("bad", ["7-8", "25:00-01:00", "01:60-02:00", "0100-0200", "01:00_02:00"])
def test_parse_rejects_malformed(bad):
    with pytest.raises(ValueError):
        parse_quiet_hours(bad)

def test_same_day_window_inclusive_start_exclusive_end():
    w = parse_quiet_hours("00:00-07:00")
    assert in_quiet_hours(_at(0, 0), w) is True     # inclusive start
    assert in_quiet_hours(_at(3), w) is True
    assert in_quiet_hours(_at(6, 59), w) is True
    assert in_quiet_hours(_at(7, 0), w) is False    # exclusive end
    assert in_quiet_hours(_at(12), w) is False

def test_midnight_wrap_window():
    w = parse_quiet_hours("23:00-07:00")
    assert in_quiet_hours(_at(23, 30), w) is True
    assert in_quiet_hours(_at(2), w) is True
    assert in_quiet_hours(_at(7, 0), w) is False
    assert in_quiet_hours(_at(12), w) is False

def test_none_and_equal_bounds_never_quiet():
    assert in_quiet_hours(_at(3), None) is False
    assert in_quiet_hours(_at(3), (120, 120)) is False   # start == end -> never

def test_element_shape():
    els = build_filler_elements("nyan_72x16.anim", timeout_s=2)
    assert els == [{"id": ELEMENT_ID, "type": "animation", "path": "nyan_72x16.anim",
                    "x": 0, "y": 0, "loop": True, "timeout": 2}]
