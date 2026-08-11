"""Who gets credited for a sync.

The string these produce ends up in the session file, in every history
listing, in the drift warning the other person reads, and in the Discord
message. Four places where a bad value is visible for a long time and
awkward to correct after the fact.
"""
from dawbridge.gui import display_name, short_name


def test_a_name_replaces_the_generic_identity():
    assert display_name("Travis", "reaper") == "Travis@reaper"
    assert short_name("Travis") == "Travis"


def test_no_name_keeps_the_old_behaviour():
    # The field is optional. An existing install with no name set must
    # carry on working exactly as before rather than crediting "".
    assert display_name("", "protools") == "gui@protools"
    assert display_name("   ", "reaper") == "gui@reaper"


def test_whitespace_is_normalised_not_preserved():
    # Someone pastes a name with a newline; it lands in session.json and
    # in a Discord message, both of which are line-oriented.
    assert display_name("  Travis   Widner \n", "reaper") == "Travis Widner@reaper"
    assert short_name("\tConner\n") == "Conner"


def test_a_very_long_name_is_bounded():
    # session.json and the status pane both render this inline.
    assert len(display_name("x" * 200, "reaper")) == 40 + len("@reaper")


def test_a_nameless_person_is_still_addressable_in_a_message():
    # "Someone published r12" reads better than "published r12".
    assert short_name("") == "Someone"


def test_the_daw_survives_in_the_session_credit():
    # History shows updated_by; knowing which DAW it came from is half
    # the value of the line.
    assert display_name("Travis", "protools").endswith("@protools")
