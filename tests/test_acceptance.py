import unittest

from creative_program_foundation.acceptance import run
from creative_program_foundation.review_acceptance import run as review_run


class AcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertFalse(result["first_replayed"])
        self.assertTrue(result["second_replayed"])
        self.assertEqual(1, result["records"])

    def test_review_offline_acceptance(self):
        result = review_run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertEqual(2, result["versions"])
        self.assertEqual(2, result["delta_rank_changes"])
        self.assertEqual(2, result["affected_notifications"])


if __name__ == "__main__":
    unittest.main()
