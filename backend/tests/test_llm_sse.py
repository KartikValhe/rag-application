"""
Step 6a-3 (docs/phase6-plan.md 7.2, 7.3): the Server-Sent Events line parser the adapters use.

It yields the data of each finished event, ignores comments and fields it does not need, and refuses a stream
that is not valid UTF-8 or has a line that never ends.
"""

import pytest

from app.llm.sse import MAX_LINE_BYTES, SSEError, SSEParser


def parse(*chunks: bytes) -> list[str]:
    parser = SSEParser()
    events: list[str] = []
    for chunk in chunks:
        events.extend(parser.feed(chunk))
    return events


def test_one_event_per_blank_line() -> None:
    assert parse(b"data: one\n\ndata: two\n\n") == ["one", "two"]


def test_only_one_leading_space_is_removed() -> None:
    assert parse(b"data:  two spaces\n\n") == [" two spaces"]
    assert parse(b"data:nospace\n\n") == ["nospace"]


def test_comment_lines_are_ignored_and_do_not_end_an_event() -> None:
    assert parse(b": OPENROUTER PROCESSING\n\ndata: x\n: another\n\n") == ["x"]


def test_several_data_lines_join_with_a_newline() -> None:
    assert parse(b"data: a\ndata: b\n\n") == ["a\nb"]


@pytest.mark.parametrize("eol", [b"\n", b"\r\n", b"\r"])
def test_every_line_ending_works(eol: bytes) -> None:
    assert parse(b"data: a" + eol + eol + b"data: b" + eol + eol) == ["a", "b"]


def test_other_fields_are_ignored() -> None:
    assert parse(b"event: message\nid: 7\nretry: 100\ndata: x\n\n") == ["x"]


def test_an_event_without_data_is_skipped() -> None:
    assert parse(b"event: ping\n\n\n\ndata: x\n\n") == ["x"]


def test_a_data_line_without_a_colon_is_an_empty_data_line() -> None:
    assert parse(b"data\ndata: x\n\n") == ["\nx"]


def test_a_byte_order_mark_at_the_start_is_dropped() -> None:
    assert parse(b"\xef\xbb\xbfdata: x\n\n") == ["x"]


def test_an_unfinished_event_at_the_end_is_dropped() -> None:
    # SSE rule: an event counts only when its blank line arrived. The adapter then sees a truncated stream.
    assert parse(b"data: done\n\ndata: cut off") == ["done"]
    assert parse(b"data: cut off\n") == []


SAMPLE = (
    ": comment\r\n"
    "data: {\"a\": \"नमस्कार \U0001f600\"}\r\n"
    "\r\n"
    "data: second\n"
    "data: line\n"
    "\n"
    "data: [DONE]\r\n"
    "\r\n"
).encode()


def test_splitting_the_bytes_anywhere_gives_the_same_events() -> None:
    whole = parse(SAMPLE)
    assert len(whole) == 3
    for cut in range(1, len(SAMPLE)):  # every split point, including inside a multi-byte character and inside CRLF
        assert parse(SAMPLE[:cut], SAMPLE[cut:]) == whole, cut
    assert parse(*(SAMPLE[i : i + 1] for i in range(len(SAMPLE)))) == whole  # one byte at a time


def test_a_crlf_split_between_chunks_is_not_two_line_ends() -> None:
    assert parse(b"data: a\r", b"\n\r", b"\ndata: b\r\n\r\n") == ["a", "b"]


def test_bytes_that_are_not_utf8_are_refused() -> None:
    with pytest.raises(SSEError):
        parse(b"data: \xff\xfe\n\n")


def test_a_character_cut_off_at_the_end_of_the_stream_is_not_an_error_by_itself() -> None:
    assert parse(b"data: ok\n\ndata: \xe0\xa4") == ["ok"]


def test_a_line_that_never_ends_is_refused() -> None:
    parser = SSEParser()
    with pytest.raises(SSEError):
        for _ in range(MAX_LINE_BYTES // 1024 + 2):
            parser.feed(b"x" * 1024)


def test_a_long_line_that_does_end_in_time_is_fine() -> None:
    payload = b"a" * (MAX_LINE_BYTES - 100)
    assert parse(b"data: " + payload + b"\n\n") == [payload.decode()]
