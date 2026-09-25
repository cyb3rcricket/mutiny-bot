"""Fresh imports and startup checks must not open a non-loopback connection."""

import os
import subprocess
import sys
import textwrap
import unittest

from config import bind_host_error


class PrivacyEgressTests(unittest.TestCase):
    def test_non_loopback_bind_is_rejected(self) -> None:
        self.assertIsNotNone(bind_host_error("0.0.0.0"))
        self.assertIsNone(bind_host_error("127.0.0.1"))

    def test_fresh_import_does_not_dial_out(self) -> None:
        script = textwrap.dedent(
            """
            import os
            import socket
            os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
            os.environ["ANONYMIZED_TELEMETRY"] = "False"
            os.environ["DO_NOT_TRACK"] = "1"
            os.environ["OLLAMA_HOST"] = "127.0.0.1:11434"
            blocked = []
            real = socket.getaddrinfo
            def wrapped(host, *args, **kwargs):
                text = str(host or "")
                if text not in {"127.0.0.1", "localhost", "::1", ""}:
                    blocked.append(text)
                    raise OSError("blocked non-loopback lookup")
                return real(host, *args, **kwargs)
            socket.getaddrinfo = wrapped
            from core.privacy import bootstrap
            bootstrap()
            import llm.llm_handler
            import web.app
            web.app.create_app
            print("BLOCKED", blocked)
            """
        )
        env = os.environ.copy()
        env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("BLOCKED []", completed.stdout)
