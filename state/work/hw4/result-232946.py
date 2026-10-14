import pytest

from solution import control_state

RUN = "COURSE-TEAM CONTROL: RUNNING"
PAUSE = "COURSE-TEAM CONTROL: PAUSED"

def test_running_marker_on_first_line():
    assert control_state(RUN) == "RUNNING"

def test_paused_marker_on_first_line():
    assert control_state(PAUSE) == "PAUSED"

def test_marker_followed_by_other_lines_still_detected():
    assert control_state(f"{RUN}\nsome details\nmore text") == "RUNNING"
    assert control_state(f"{PAUSE}\nclass cancelled") == "PAUSED"

def test_unrelated_first_line_is_unknown():
    assert control_state("Welcome to the forum") == "UNKNOWN"

def test_none_input_is_unknown():
    assert control_state(None) == "UNKNOWN"

def test_empty_string_is_unknown():
    assert control_state("") == "UNKNOWN"

def test_whitespace_only_text_is_unknown():
    assert control_state("   \n\t\n  ") == "UNKNOWN"

def test_leading_blank_lines_are_skipped():
    assert control_state(f"\n\n   \n{RUN}") == "RUNNING"
    assert control_state(f"\n{PAUSE}\nnotes") == "PAUSED"

def test_surrounding_spaces_and_tabs_are_stripped():
    assert control_state(f"   {RUN}   ") == "RUNNING"
    assert control_state(f"\t{PAUSE}\t") == "PAUSED"

def test_only_first_nonempty_line_decides():
    # A marker on a later line must not count when the first
    # non-empty line is something else.
    assert control_state(f"Hello\n{RUN}") == "UNKNOWN"
    assert control_state(f"Notice\n{PAUSE}") == "UNKNOWN"

def test_match_is_case_insensitive():
    assert control_state("course-team control: running") == "RUNNING"
    assert control_state("Course-Team Control: Paused") == "PAUSED"

def test_extra_text_on_marker_line_is_unknown():
    assert control_state(f"{RUN} now") == "UNKNOWN"
    assert control_state(f"{PAUSE}!") == "UNKNOWN"

def test_crlf_line_endings_are_handled():
    assert control_state(f"{RUN}\r\nbody") == "RUNNING"
    assert control_state(f"\r\n{PAUSE}\r\n") == "PAUSED"

@pytest.mark.parametrize(
    "text",
    [None, "", "random", f"{RUN}x", f"x\n{PAUSE}", "\n\n"],
)
def test_result_is_always_one_of_three_values(text):
    assert control_state(text) in {"RUNNING", "PAUSED", "UNKNOWN"}
