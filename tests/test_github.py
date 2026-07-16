import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from agent_society_loop.github import GitHubIssueClient


class GitHubHandler(BaseHTTPRequestHandler):
    status = 200
    body = {
        "number": 12,
        "title": "Fix the parser",
        "body": "Parser fails on empty input",
        "labels": [{"name": "bug"}],
        "html_url": "https://github.com/owner/repo/issues/12",
        "state": "open",
    }
    received = None

    def do_GET(self):
        type(self).received = {
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "user_agent": self.headers.get("User-Agent"),
        }
        payload = json.dumps(type(self).body).encode("utf-8")
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        return


class GitHubIssueClientTests(unittest.TestCase):
    def setUp(self):
        GitHubHandler.status = 200
        GitHubHandler.body = {
            "number": 12,
            "title": "Fix the parser",
            "body": "Parser fails on empty input",
            "labels": [{"name": "bug"}],
            "html_url": "https://github.com/owner/repo/issues/12",
            "state": "open",
        }
        GitHubHandler.received = None
        self.server = HTTPServer(("127.0.0.1", 0), GitHubHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_get_issue_normalizes_public_github_response(self):
        issue = GitHubIssueClient(self.base_url, token="test-token").get_issue(
            "owner/repo", 12
        )

        self.assertEqual(issue.repository, "owner/repo")
        self.assertEqual(issue.number, 12)
        self.assertEqual(issue.labels, ("bug",))
        self.assertEqual(GitHubHandler.received["path"], "/repos/owner/repo/issues/12")
        self.assertEqual(GitHubHandler.received["authorization"], "Bearer test-token")
        self.assertIn("agent-society-loop", GitHubHandler.received["user_agent"])

    def test_rejects_invalid_repository_and_issue_number(self):
        client = GitHubIssueClient(self.base_url)
        for repository, number in (("owner", 1), ("owner/repo/extra", 1), ("a b/repo", 1), ("owner/repo", 0)):
            with self.subTest(repository=repository, number=number):
                with self.assertRaises(ValueError):
                    client.get_issue(repository, number)

    def test_http_error_does_not_expose_token(self):
        GitHubHandler.status = 503
        client = GitHubIssueClient(self.base_url, token="do-not-leak")

        with self.assertRaises(RuntimeError) as raised:
            client.get_issue("owner/repo", 12)

        self.assertIn("503", str(raised.exception))
        self.assertNotIn("do-not-leak", str(raised.exception))

    def test_response_size_limit_is_enforced(self):
        GitHubHandler.body = {**GitHubHandler.body, "body": "x" * 500}
        client = GitHubIssueClient(self.base_url, max_response_bytes=100)

        with self.assertRaisesRegex(ValueError, "too large"):
            client.get_issue("owner/repo", 12)


if __name__ == "__main__":
    unittest.main()
