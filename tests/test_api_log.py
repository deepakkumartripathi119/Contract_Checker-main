"""The API log file is append-only, and a laptop run does not require one."""

import os
import tempfile
import unittest
from unittest.mock import patch

from scripts.api_server import build_log_config


class TestApiLog(unittest.TestCase):
    def test_a_laptop_run_does_not_open_a_log_file(self):
        with patch.dict(os.environ, {"API_LOG_DIR": ""}, clear=False):
            config = build_log_config()
        self.assertNotIn("file", config["handlers"])

    def test_the_api_log_file_appends(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"API_LOG_DIR": directory}, clear=False):
                config = build_log_config()
            path = config["handlers"]["file"]["filename"]
            self.assertTrue(path.startswith(directory))
            self.assertIn("api-", os.path.basename(path))
            self.assertEqual(config["handlers"]["file"]["mode"], "a")
            self.assertEqual(config["handlers"]["access_file"]["filename"], path)
            self.assertIn("file", config["loggers"]["uvicorn"]["handlers"])
            self.assertIn("access_file", config["loggers"]["uvicorn.access"]["handlers"])
