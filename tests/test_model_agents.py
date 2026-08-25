import unittest

from seed_society.domain import Goal, Task, ToolRisk, Verdict
from seed_society.model_agents import (
    ModelPlanner,
    ModelReviewer,
    ModelWorker,
    parse_json_object,
)
from seed_society.storage import SQLiteRepository
from seed_society.tools import (
    DefaultToolPolicy,
    ToolExecutor,
    ToolRegistry,
)


class ScriptedProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, *, temperature=0.0):
        self.calls.append({"messages": list(messages), "temperature": temperature})
        if not self.responses:
            raise AssertionError("provider script exhausted")
        return self.responses.pop(0)


class EchoTool:
    name = "echo"
    description = "Echo text"
    risk = ToolRisk.READ
    input_schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    }

    def invoke(self, arguments):
        return arguments["text"]


class StrictModelAgentTests(unittest.TestCase):
    def test_parse_json_object_rejects_markdown_and_surrounding_text(self):
        for value in (
            '```json\n{"tasks": []}\n```',
            'result: {"tasks": []}',
            '{"tasks": []} trailing',
            "[]",
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "exactly one JSON object"):
                    parse_json_object(value)

    def test_model_planner_returns_validated_typed_tasks(self):
        provider = ScriptedProvider(
            [
                '{"tasks":[{"task_id":"inspect","task_type":"repo",'
                '"description":"Inspect repository","dependencies":[],'
                '"acceptance_criteria":{"required_terms":["evidence"]}}]}'
            ]
        )

        tasks = ModelPlanner(provider).plan(
            Goal.create("Maintain", "Resolve issue", "goal"), {"knowledge": []}
        )

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].goal_id, "goal")
        self.assertEqual(tasks[0].task_id, "inspect")
        self.assertEqual(tasks[0].position, 1)
        self.assertIn("strict JSON", provider.calls[0]["messages"][0]["content"])

    def test_model_planner_rejects_schema_and_graph_errors(self):
        missing = ScriptedProvider(
            ['{"tasks":[{"task_id":"x","description":"Inspect"}]}']
        )
        with self.assertRaisesRegex(ValueError, "task_type"):
            ModelPlanner(missing).plan(Goal.create("x", "y", "g"), {})

        cycle = ScriptedProvider(
            [
                '{"tasks":['
                '{"task_id":"a","task_type":"x","description":"a","dependencies":["b"]},'
                '{"task_id":"b","task_type":"x","description":"b","dependencies":["a"]}'
                "]}"
            ]
        )
        with self.assertRaisesRegex(ValueError, "cycle"):
            ModelPlanner(cycle).plan(Goal.create("x", "y", "g"), {})

    def test_model_reviewer_returns_structured_review(self):
        provider = ScriptedProvider(
            [
                '{"verdict":"FAIL","score":42,"defects":['
                '{"location":"result","issue":"missing evidence",'
                '"suggestion":"cite a file"}],"summary":"Needs work"}'
            ]
        )
        task = Task.create("g", "t", "analysis", "Analyze")

        review = ModelReviewer(provider).review(task, "draft", 2)

        self.assertEqual(review.verdict, Verdict.FAIL)
        self.assertEqual(review.attempt_no, 2)
        self.assertEqual(review.defects[0].issue, "missing evidence")

    def test_model_worker_executes_tool_loop_then_returns_artifact(self):
        repository = SQLiteRepository(":memory:")
        provider = ScriptedProvider(
            [
                '{"type":"tool_call","name":"echo","arguments":{"text":"evidence"}}',
                '{"type":"final","content":"proposal with evidence"}',
            ]
        )
        executor = ToolExecutor(
            ToolRegistry([EchoTool()]), DefaultToolPolicy(), repository
        )
        worker = ModelWorker("maintainer", provider, executor)

        artifact = worker.execute(
            Task.create("g", "t", "maintenance", "Propose fix"), {"goal": {}}
        )

        self.assertEqual(artifact, "proposal with evidence")
        self.assertIn("tool_result", provider.calls[1]["messages"][-1]["content"])
        self.assertIn("evidence", provider.calls[1]["messages"][-1]["content"])
        repository.close()

    def test_model_worker_bounds_tool_steps(self):
        repository = SQLiteRepository(":memory:")
        provider = ScriptedProvider(
            [
                '{"type":"tool_call","name":"echo","arguments":{"text":"one"}}',
                '{"type":"tool_call","name":"echo","arguments":{"text":"two"}}',
            ]
        )
        worker = ModelWorker(
            "maintainer",
            provider,
            ToolExecutor(
                ToolRegistry([EchoTool()]), DefaultToolPolicy(), repository
            ),
            max_tool_steps=1,
        )

        with self.assertRaisesRegex(RuntimeError, "tool step budget"):
            worker.execute(
                Task.create("g", "t", "maintenance", "Propose fix"), {}
            )
        repository.close()


if __name__ == "__main__":
    unittest.main()
