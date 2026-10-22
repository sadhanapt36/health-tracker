from health_tracker.whatsapp.normalize import parse_pre_plain_text, to_raw_message


def test_parses_us_12h_with_narrow_nbsp():
    raw, iso, sender = parse_pre_plain_text("[3:45\u202fPM, 10/21/2026] Grandpa: ")
    assert iso == "2026-10-21T15:45"
    assert sender == "Grandpa"
    assert raw == "3:45 PM, 10/21/2026"


def test_parses_24h():
    _, iso, _ = parse_pre_plain_text("[15:45, 10/21/2026] Grandpa: ")
    assert iso == "2026-10-21T15:45"


def test_unparseable_time_keeps_raw_and_leaves_iso_blank():
    raw, iso, sender = parse_pre_plain_text("[yesterday, 3pm] Grandpa: ")
    assert raw == "yesterday, 3pm" and iso is None and sender == "Grandpa"


def test_missing_attribute_gives_all_none():
    assert parse_pre_plain_text(None) == (None, None, None)


def test_to_raw_message_handles_image_without_text():
    m = to_raw_message({"id": "x1", "prePlain": None, "text": None, "hasImage": True})
    assert m.text == "" and m.has_image and m.source_message_id == "x1"



def test_row_text_fallback_for_forwarded_text():
    m = to_raw_message({"id": "a", "prePlain": None, "text": "",
                        "rowText": "Forwarded\nLB149.4\n10:02\u202fPM"})
    assert m.text == "LB149.4"
    assert m.sent_at_raw == "10:02 PM"
    assert m.sent_at is None  # time of day only: no date, so don't invent one


def test_row_text_fallback_ignores_album_counter():
    m = to_raw_message({"id": "b", "prePlain": None, "text": "",
                        "rowText": "+2\n10:02 PM", "hasImage": True})
    assert m.text == "" and m.has_image


def test_attribute_wins_over_fallback():
    m = to_raw_message({"id": "c", "prePlain": "[3:45 PM, 10/21/2026] Amma: ",
                        "text": "BP 120/80", "rowText": "something else\n9:99"})
    assert m.text == "BP 120/80" and m.sent_at == "2026-10-21T15:45"


def test_merge_keeps_filled_row_over_later_empty_shell():
    from health_tracker.whatsapp.normalize import merge_best_rows
    best = {}
    merge_best_rows(best, [{"id": "a", "rowText": ""}, {"id": "b", "rowText": "hi\n10:02 PM"}])
    order = merge_best_rows(best, [{"id": "a", "rowText": "LB149.4\n10:02 PM"},
                                   {"id": "b", "rowText": ""}])
    assert order == ["a", "b"]
    assert best["a"]["rowText"].startswith("LB149.4")   # shell got filled in
    assert best["b"]["rowText"].startswith("hi")         # filled row not blanked
