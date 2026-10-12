# Mutant 2 (SYL-113, used by verify.py, HW4-4): no strip or upper-casing, so "  course-team control: running"
# and a line with trailing spaces come back UNKNOWN.
RUNNING_LINE = "COURSE-TEAM CONTROL: RUNNING"
PAUSED_LINE = "COURSE-TEAM CONTROL: PAUSED"


def control_state(text: str) -> str:
    """RUNNING / PAUSED / UNKNOWN from the first non-empty line of a forum description."""
    for line in (text or "").splitlines():
        if line.strip():
            first = line
            if first == RUNNING_LINE:
                return "RUNNING"
            if first == PAUSED_LINE:
                return "PAUSED"
            return "UNKNOWN"
    return "UNKNOWN"
