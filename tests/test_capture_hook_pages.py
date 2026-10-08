from tools.capture_hook_pages import _changed_spans


def test_changed_spans_accepts_regions_that_shrink_or_grow() -> None:
    assert _changed_spans(b"abcd", b"ab") == [(2, 4)]
    assert _changed_spans(b"ab", b"abcd") == [(2, 4)]
    assert _changed_spans(b"abcd", b"ax") == [(1, 4)]


def test_changed_spans_keeps_equal_length_behavior() -> None:
    assert _changed_spans(b"abcdef", b"abXdeY") == [(2, 3), (5, 6)]
