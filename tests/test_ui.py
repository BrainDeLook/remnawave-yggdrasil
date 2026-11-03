import contextlib
import io
import unittest
from unittest.mock import patch
from ui import confirm


class ConfirmationTests(unittest.TestCase):
    def test_empty_input_does_not_authorize_installation(self):
        with contextlib.redirect_stdout(io.StringIO()),patch('builtins.input',return_value=''):
            self.assertFalse(confirm('Применить план?'))
    def test_typo_requires_explicit_answer_before_installation(self):
        with contextlib.redirect_stdout(io.StringIO()),patch('builtins.input',side_effect=['lf','да']) as read:
            self.assertTrue(confirm('Применить план?'))
        self.assertEqual(read.call_count,2)

if __name__=='__main__': unittest.main()
