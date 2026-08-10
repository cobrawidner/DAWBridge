"""Pro Tools should land on a Bars|Beats grid, not Min:Secs.

A fresh Pro Tools session defaults to Min:Secs, so the grid you snap to
doesn't line up with the bars anyone is playing to - while Reaper is on
bars/beats by default. Two people end up reading different rulers for the
same song.
"""
from dawbridge.protools_backend import ProToolsBackend


class _FakeCounter:
    def __init__(self, setting):
        self.current_setting = setting


class _FakeEngine:
    """Mimics just the counter calls, using the real enum values."""

    def __init__(self, setting):
        self.setting = setting
        self.set_calls = []

    def get_main_counter_format(self):
        return _FakeCounter(self.setting)

    def set_main_counter_format(self, new_loc_type):
        self.set_calls.append(new_loc_type)
        self.setting = _BARS_BEATS


_BARS_BEATS = 1   # TrackOffsetOptions.BarsBeats
_MIN_SECS = 2     # TrackOffsetOptions.MinSecs


def test_switches_from_min_secs_and_says_so():
    engine = _FakeEngine(_MIN_SECS)
    warnings: list[str] = []

    ProToolsBackend()._use_bars_beats_counter(engine, warnings)

    assert len(engine.set_calls) == 1
    assert any("Bars|Beats" in w for w in warnings)


def test_leaves_bars_beats_alone_and_stays_quiet():
    # Repeat pushes shouldn't re-set it or add noise to the log.
    engine = _FakeEngine(_BARS_BEATS)
    warnings: list[str] = []

    ProToolsBackend()._use_bars_beats_counter(engine, warnings)

    assert engine.set_calls == []
    assert warnings == []


def test_counter_failure_is_reported_not_raised():
    # Losing the grid preference must never abort a push.
    class Broken:
        def get_main_counter_format(self):
            raise RuntimeError("PTSL said no")

    warnings: list[str] = []
    ProToolsBackend()._use_bars_beats_counter(Broken(), warnings)

    assert len(warnings) == 1 and "could not switch" in warnings[0]
