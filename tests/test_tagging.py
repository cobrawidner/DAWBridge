from dawbridge.tagging import parse_tag, strip_tag, tag


def test_tag_and_parse_round_trip():
    tagged = tag("Lead Vocal", "a1b2c3d4")
    assert tagged == "Lead Vocal #a1b2c3d4"
    base, obj_id = parse_tag(tagged)
    assert base == "Lead Vocal"
    assert obj_id == "a1b2c3d4"


def test_parse_untagged_name():
    base, obj_id = parse_tag("Some Local Track")
    assert base == "Some Local Track"
    assert obj_id is None


def test_retagging_replaces_old_tag():
    once = tag("Kick", "11112222")
    twice = tag(once, "33334444")
    assert twice == "Kick #33334444"


def test_strip_tag():
    assert strip_tag("Snare #deadbeef") == "Snare"
    assert strip_tag("Snare") == "Snare"


def test_tag_requires_exactly_8_hex_chars():
    # A trailing "#word" that isn't 8 hex chars shouldn't be parsed as a tag.
    base, obj_id = parse_tag("Guitar #solo")
    assert obj_id is None
    assert base == "Guitar #solo"
