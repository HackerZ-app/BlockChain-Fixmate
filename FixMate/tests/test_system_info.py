import time
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import app  # noqa: E402


class SystemInfoTests(unittest.TestCase):
    def test_system_info_endpoint_is_fast_enough(self):
        client = app.test_client()
        start = time.time()
        response = client.get('/system-info')
        elapsed = time.time() - start

        self.assertEqual(response.status_code, 200)
        self.assertLess(
            elapsed,
            4.0,
            f"/system-info too slow: {elapsed:.3f}s",
        )


if __name__ == '__main__':
    unittest.main()
