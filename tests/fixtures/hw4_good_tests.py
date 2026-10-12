# A delivered test file that passes every HW4-4 check (SYL-115): the real function passes, both
# mutants fail. The name does not start with "test", so the suite's discovery never runs it here.
import unittest

from control_state import control_state


class TestControlState(unittest.TestCase):
    def test_running_line(self):
        self.assertEqual(control_state("COURSE-TEAM CONTROL: RUNNING\nhello"), "RUNNING")

    def test_paused_line(self):
        self.assertEqual(control_state("COURSE-TEAM CONTROL: PAUSED"), "PAUSED")

    def test_whitespace_and_case_are_normalized(self):
        self.assertEqual(control_state("\n  course-team control: running  \n"), "RUNNING")

    def test_empty_is_unknown(self):
        self.assertEqual(control_state(""), "UNKNOWN")
        self.assertEqual(control_state(None), "UNKNOWN")

    def test_only_the_first_non_empty_line_counts(self):
        self.assertEqual(control_state("hello\nCOURSE-TEAM CONTROL: RUNNING"), "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
