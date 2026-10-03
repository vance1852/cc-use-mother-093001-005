import unittest

from creative_program_foundation.advancement_acceptance import run


class AdvancementAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(result["replay_match"])
        self.assertTrue(result["restart_continued"])
        self.assertEqual([1, 2], result["published_versions"])


if __name__ == "__main__":
    unittest.main()
