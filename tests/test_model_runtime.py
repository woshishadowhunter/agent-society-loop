import json
import tempfile
import unittest
from pathlib import Path

from seed_society.model_agents import ModelReviewer, ModelWorker
from seed_society.model_runtime import (
    ModelRuntime,
    ModelProviderConfig,
    doctor_model_runtime,
    load_model_runtime,
)


class ProbeProvider:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    def complete(self, messages, *, temperature=0.0):
        if self.error is not None:
            raise self.error
        return self.response


class ModelRuntimeConfigTests(unittest.TestCase):
    def write_config(self, root, value):
        path = Path(root) / "model-agents.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    @staticmethod
    def valid_config():
        return {
            "providers": {
                "local": {
                    "base_url": "http://127.0.0.1:11434/v1",
                    "model": "local-small",
                    "timeout_seconds": 45,
                    "structured_output": True,
                    "allow_insecure_http": False,
                }
            },
            "agents": [
                {
                    "agent_id": "local-writer",
                    "provider": "local",
                    "task_types": ["writing", "summary"],
                    "max_tool_steps": 4,
                }
            ],
            "reviewer": {
                "agent_id": "local-reviewer",
                "provider": "local",
            },
        }

    def test_loads_model_workers_reviewer_assignments_and_exact_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(directory, self.valid_config())

            runtime = load_model_runtime(path, {})

        self.assertEqual(set(runtime.workers), {"local-writer"})
        self.assertIsInstance(runtime.workers["local-writer"], ModelWorker)
        self.assertIsInstance(runtime.reviewer, ModelReviewer)
        self.assertEqual(
            runtime.assignments,
            {"summary": "local-writer", "writing": "local-writer"},
        )
        self.assertEqual(len(runtime.profiles), 2)
        profiles = {profile.agent_id: profile for profile in runtime.profiles}
        profile = profiles["local-writer"]
        self.assertEqual(profile.agent_id, "local-writer")
        self.assertEqual(profile.task_types, ("summary", "writing"))
        self.assertEqual(
            profile.model_id,
            runtime.providers["local"].identity,
        )
        reviewer = profiles["local-reviewer"]
        self.assertEqual(reviewer.role, "reviewer")
        self.assertEqual(reviewer.model_id, runtime.providers["local"].identity)
        self.assertEqual(runtime.reviewer.agent_id, "local-reviewer")

    def test_rejects_duplicate_task_type_ownership(self):
        config = self.valid_config()
        config["agents"].append(
            {
                "agent_id": "second-writer",
                "provider": "local",
                "task_types": ["writing"],
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(directory, config)

            with self.assertRaisesRegex(ValueError, "writing.*multiple agents"):
                load_model_runtime(path, {})

    def test_rejects_unknown_keys_and_missing_provider_references(self):
        for mutation, message in (
            (lambda value: value.update({"secret": "no"}), "unknown model runtime keys"),
            (
                lambda value: value["agents"][0].update({"provider": "missing"}),
                "unknown provider",
            ),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                config = self.valid_config()
                mutation(config)
                path = self.write_config(directory, config)

                with self.assertRaisesRegex(ValueError, message):
                    load_model_runtime(path, {})

    def test_remote_provider_requires_present_secret_environment(self):
        config = self.valid_config()
        config["providers"]["local"] = {
            "base_url": "https://models.example/v1",
            "model": "remote-small",
            "api_key_env": "MODEL_SECRET",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(directory, config)

            with self.assertRaisesRegex(ValueError, "MODEL_SECRET"):
                load_model_runtime(path, {})

            runtime = load_model_runtime(path, {"MODEL_SECRET": "not-persisted"})

        self.assertEqual(runtime.providers["local"].api_key_env, "MODEL_SECRET")
        self.assertNotIn("not-persisted", repr(runtime))

    def test_non_loopback_http_requires_explicit_config_opt_in(self):
        config = self.valid_config()
        config["providers"]["local"].update(
            {
                "base_url": "http://host.docker.internal:11434/v1",
                "api_key_env": "MODEL_SECRET",
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(directory, config)

            with self.assertRaisesRegex(ValueError, "HTTPS"):
                load_model_runtime(path, {"MODEL_SECRET": "secret"})

            config["providers"]["local"]["allow_insecure_http"] = True
            path = self.write_config(directory, config)
            runtime = load_model_runtime(path, {"MODEL_SECRET": "secret"})

        self.assertTrue(runtime.providers["local"].allow_insecure_http)

    def test_doctor_requires_exact_structured_probe_response(self):
        config = ModelProviderConfig(
            "local", "http://127.0.0.1:11434/v1", "local-small"
        )
        runtime = ModelRuntime(
            providers={"local": config},
            workers={},
            assignments={},
            reviewer=None,
            profiles=(),
            provider_clients={"local": ProbeProvider('{"status":"ok"}')},
        )

        report = doctor_model_runtime(runtime)

        self.assertTrue(report["passed"])
        self.assertEqual(report["providers"][0]["name"], "local")
        self.assertTrue(report["providers"][0]["passed"])
        self.assertNotIn("api_key_env", report["providers"][0])

        runtime.provider_clients["local"] = ProbeProvider('{"status":"almost"}')
        failed = doctor_model_runtime(runtime)
        self.assertFalse(failed["passed"])
        self.assertEqual(failed["providers"][0]["error_category"], "ValueError")

    def test_doctor_sanitizes_provider_failures(self):
        config = ModelProviderConfig(
            "local", "http://127.0.0.1:11434/v1", "local-small"
        )
        runtime = ModelRuntime(
            providers={"local": config},
            workers={},
            assignments={},
            reviewer=None,
            profiles=(),
            provider_clients={
                "local": ProbeProvider(error=RuntimeError("secret response body"))
            },
        )

        report = doctor_model_runtime(runtime)

        self.assertFalse(report["passed"])
        self.assertEqual(report["providers"][0]["error_category"], "RuntimeError")
        self.assertNotIn("secret response body", repr(report))


if __name__ == "__main__":
    unittest.main()
