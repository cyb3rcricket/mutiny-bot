"""Fresh imports and startup checks must not open a non-loopback connection."""

import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest

import yaml

from config import bind_host_error, searxng_endpoint_error


ROOT = Path(__file__).resolve().parents[1]


class PrivacyEgressTests(unittest.TestCase):
    def test_outbound_default_and_truthy_values_are_explicit(self) -> None:
        script = textwrap.dedent(
            """
            import importlib
            import os
            import dotenv
            dotenv.load_dotenv = lambda *args, **kwargs: None
            import config
            for value in (None, "1", "true", "yes", "0", "false", "no", ""):
                if value is None:
                    os.environ.pop("MUTINY_OUTBOUND_ENABLED", None)
                else:
                    os.environ["MUTINY_OUTBOUND_ENABLED"] = value
                importlib.reload(config)
                print(f"{value!r}:{config.OUTBOUND_ENABLED}")
            """
        )
        env = os.environ.copy()
        env.pop("MUTINY_OUTBOUND_ENABLED", None)
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            completed.stdout.splitlines(),
            [
                "None:True",
                "'1':True",
                "'true':True",
                "'yes':True",
                "'0':False",
                "'false':False",
                "'no':False",
                "'':False",
            ],
        )

    def test_searxng_endpoint_stays_loopback_only(self) -> None:
        self.assertIsNone(searxng_endpoint_error("http://127.0.0.1:8080"))
        self.assertIsNone(searxng_endpoint_error("http://localhost:8080"))
        self.assertIsNone(searxng_endpoint_error("http://[::1]:8080"))
        self.assertIsNotNone(searxng_endpoint_error("https://public.example/search"))

    def test_searxng_compose_contract_is_loopback_only(self) -> None:
        compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        self.assertEqual(set(compose["services"]), {"searxng"})
        service = compose["services"]["searxng"]
        self.assertEqual(service["image"], "searxng/searxng:latest")
        self.assertEqual(service["ports"], ["127.0.0.1:8080:8080"])
        self.assertEqual(service["volumes"], ["./searxng/settings.yml:/etc/searxng/settings.yml:ro"])
        self.assertNotIn("redis", str(compose).lower())
        environment = service["environment"]
        self.assertEqual(environment["SEARXNG_BASE_URL"], "http://127.0.0.1:8080/")
        self.assertEqual(environment["FORCE_OWNERSHIP"], "false")
        self.assertGreaterEqual(len(environment["SEARXNG_SECRET"]), 48)

        settings = yaml.safe_load((ROOT / "searxng/settings.yml").read_text(encoding="utf-8"))
        self.assertTrue(settings["use_default_settings"])
        self.assertEqual(settings["search"]["formats"], ["html", "json"])
        self.assertEqual(settings["server"]["bind_address"], "0.0.0.0")
        self.assertFalse(settings["server"]["limiter"])
        self.assertFalse(settings["server"]["public_instance"])
        self.assertGreaterEqual(len(settings["server"]["secret_key"]), 48)
        self.assertEqual(environment["SEARXNG_SECRET"], settings["server"]["secret_key"])

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
