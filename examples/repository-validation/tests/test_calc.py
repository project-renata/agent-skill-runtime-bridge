import unittest
from calc import add


class ArithmeticTests(unittest.TestCase):
    def test_addition(self):
        self.assertEqual(add(2, 3), 5)
