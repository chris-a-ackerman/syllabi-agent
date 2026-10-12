RUNNING_LINE = "COURSE-TEAM CONTROL: RUNNING"
PAUSED_LINE = "COURSE-TEAM CONTROL: PAUSED"


def control_state(text: str) -> str:
    """RUNNING / PAUSED / UNKNOWN from the first non-empty line of a forum description."""
    for line in (text or "").splitlines():
        if line.strip():
            first = line.strip().upper()
            if first == RUNNING_LINE:
                return "RUNNING"
            if first == PAUSED_LINE:
                return "PAUSED"
            return "UNKNOWN"
    return "UNKNOWN"
