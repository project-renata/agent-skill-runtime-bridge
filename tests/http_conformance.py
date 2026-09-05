"""Exercise a deployed adapter with the same authenticated HTTP cases.

BRIDGE_ENDPOINT and BRIDGE_API_KEY must be set. Uses the public example by default.
Optional --request JSON_FILE supplies a different allowed request (e.g. private probe).
Never prints the API key, headers, or program result contents.
"""
import argparse
import json
import os
from pathlib import Path
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request")
    args = parser.parse_args()
    body = json.loads(Path(args.request).read_text()) if args.request else {
        "repository": "project-renata/agent-skill-runtime-bridge", "ref": "main",
        "program": "examples/markdown_titles/main.py", "files": ["README.md"],
        "input": {"files": ["README.md"]}}
    endpoint, key = os.environ["BRIDGE_ENDPOINT"], os.environ["BRIDGE_API_KEY"]
    cases = [
        ("unauthorized", body, "", 401, "unauthorized"),
        ("path_escape", {**body, "files": ["../outside"]}, key, 400, "invalid_path"),
        ("unapproved_repository", {**body, "repository": "not-allowed/repository"}, key, 403, "repository_not_allowed"),
        ("unapproved_ref", {**body, "ref": "unapproved-ref"}, key, 403, "ref_not_allowed"),
        ("unapproved_program", {**body, "program": "unapproved.py"}, key, 403, "program_not_allowed"),
        ("canonical_execution", body, key, 200, None)]
    results = []
    for name, request_body, token, expected_status, expected_error in cases:
        request = urllib.request.Request(endpoint, data=json.dumps(request_body).encode(), headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                status, raw, cache = response.status, response.read(), response.headers.get("Cache-Control")
        except urllib.error.HTTPError as error:
            status, raw, cache = error.code, error.read(), error.headers.get("Cache-Control")
        response = json.loads(raw)
        assert status == expected_status, (name, status, response.get("error"))
        assert cache == "no-store", (name, "missing no-store")
        if expected_error:
            assert response["error"]["code"] == expected_error, name
        else:
            assert response["ok"] and response["result"]["count"] == len(body["input"]["files"]), name
            assert response["source"]["program"] == body["program"], name
            assert len(response["source"]["commit"]) == 40, name
            assert len(response["source"]["sha256"]) == 64, name
            assert all(item["line_count"] >= 0 for item in response["result"]["files"]), name
        results.append({"case": name, "status": "PASS", "http_status": status})
    print(json.dumps({"endpoint": endpoint, "cases": results}, indent=2))


if __name__ == "__main__":
    main()
