"""Protocol, authorization and immutable GitHub snapshot loading. No vendor SDK."""
import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from urllib.parse import quote

MAX_REQUEST = 64 * 1024
MAX_FILE = 512 * 1024
MAX_TOTAL = 2 * 1024 * 1024
MAX_FILES = 32
MAX_RESULT = 512 * 1024


@dataclass
class Execution:
    result: object
    changes: dict


class BridgeError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def safe_path(value):
    if (not isinstance(value, str) or not value or len(value) > 1024
            or "\\" in value or re.match(r"^[A-Za-z]:", value) or any(ord(c) < 32 for c in value)
            or any(p in ("", ".", "..", ".git") for p in value.split("/"))):
        raise BridgeError("invalid_path")
    return value


def under(path, prefixes):
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def write_prefixes_for(policy, ref):
    """Explicit per-ref maps replace, rather than extend, the legacy grant."""
    if "write_prefixes_by_ref" in policy:
        return policy["write_prefixes_by_ref"].get(ref, [])
    return policy.get("write_prefixes", [])


def readable(path, policy):
    return policy.get("read_all", False) or under(
        path, policy.get("data_prefixes", []) + policy["program_prefixes"])


def writable(path, policy, ref):
    return ref in policy.get("write_all_refs", []) or under(
        path, write_prefixes_for(policy, ref))


class Settings:
    def __init__(self, key, repositories, github_token=""):
        if not isinstance(key, str) or len(key) < 32:
            raise BridgeError("server_not_configured", 503)
        if not isinstance(repositories, dict) or not repositories:
            raise BridgeError("server_not_configured", 503)
        for name, policy in repositories.items():
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name):
                raise BridgeError("server_not_configured", 503)
            if not isinstance(policy, dict) or not isinstance(policy.get("ref"), str) or not policy["ref"]:
                raise BridgeError("server_not_configured", 503)
            if not isinstance(policy.get("read_all", False), bool):
                raise BridgeError("server_not_configured", 503)
            for field in ("program_prefixes", "data_prefixes"):
                prefixes = policy.get(field, [] if field == "data_prefixes" else None)
                optional = field == "data_prefixes" and policy.get("read_all", False)
                if not isinstance(prefixes, list) or (not prefixes and not optional):
                    raise BridgeError("server_not_configured", 503)
                for prefix in prefixes:
                    safe_path(prefix)
            for field in ("additional_refs", "write_refs", "write_prefixes", "write_all_refs"):
                values = policy.get(field, [])
                if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values):
                    raise BridgeError("server_not_configured", 503)
            for prefix in policy.get("write_prefixes", []):
                safe_path(prefix)
            for ref in policy.get("write_all_refs", []):
                if (ref not in policy.get("write_refs", [])
                        or ref not in [policy["ref"], *policy.get("additional_refs", [])]
                        or policy.get("write_prefixes")):
                    raise BridgeError("server_not_configured", 503)
            if "write_prefixes_by_ref" in policy:
                grants = policy["write_prefixes_by_ref"]
                if not isinstance(grants, dict) or policy.get("write_prefixes"):
                    raise BridgeError("server_not_configured", 503)
                for ref, prefixes in grants.items():
                    if (ref not in policy.get("write_refs", [])
                            or ref not in [policy["ref"], *policy.get("additional_refs", [])]
                            or ref in policy.get("write_all_refs", [])
                            or not isinstance(prefixes, list) or not prefixes):
                        raise BridgeError("server_not_configured", 503)
                    for prefix in prefixes:
                        safe_path(prefix)
            repo_files = policy.get("repo_files")
            if repo_files is not None:
                if (not isinstance(repo_files, dict) or set(repo_files) != {"ref", "program"}
                        or repo_files["ref"] not in [policy["ref"], *policy.get("additional_refs", [])]):
                    raise BridgeError("server_not_configured", 503)
                program = safe_path(repo_files["program"])
                if not program.endswith(".py") or not under(program, policy["program_prefixes"]):
                    raise BridgeError("server_not_configured", 503)
            authoring = policy.get("authoring")
            if authoring is not None:
                if (not isinstance(authoring, dict)
                        or set(authoring) != {"ref", "program", "program_prefix", "data_prefix"}
                        or authoring["ref"] not in [policy["ref"], *policy.get("additional_refs", [])]
                        or authoring["ref"] not in policy.get("write_refs", [])):
                    raise BridgeError("server_not_configured", 503)
                writer = safe_path(authoring["program"])
                programs = safe_path(authoring["program_prefix"])
                data = safe_path(authoring["data_prefix"])
                if (not writer.endswith(".py") or not under(writer, policy["program_prefixes"])
                        or not under(programs, policy["program_prefixes"])
                        or not writable(programs, policy, authoring["ref"])
                        or not readable(data, policy)
                        or not writable(data, policy, authoring["ref"])):
                    raise BridgeError("server_not_configured", 503)
        self.key, self.repositories, self.github_token = key, repositories, github_token

    @classmethod
    def from_env(cls, env):
        try:
            return cls(env.get("BRIDGE_API_KEY", ""),
                       json.loads(env.get("BRIDGE_REPOSITORIES", "{}")),
                       env.get("BRIDGE_GITHUB_TOKEN", ""))
        except (ValueError, TypeError, BridgeError):
            raise BridgeError("server_not_configured", 503) from None


def parse_request(raw, authorization, settings):
    expected = ("Bearer " + settings.key).encode()
    if not isinstance(authorization, str) or not hmac.compare_digest(authorization.encode(), expected):
        raise BridgeError("unauthorized", 401)
    if len(raw) > MAX_REQUEST:
        raise BridgeError("request_too_large", 413)
    try:
        request = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError):
        raise BridgeError("invalid_json") from None
    fields = {"repository", "ref", "program", "files", "input"}
    if not isinstance(request, dict) or set(request) not in (fields, fields | {"write"}):
        raise BridgeError("invalid_request")
    repo = request["repository"]
    if not isinstance(repo, str) or repo not in settings.repositories:
        raise BridgeError("repository_not_allowed", 403)
    policy = settings.repositories[repo]
    if request["ref"] not in [policy["ref"], *policy.get("additional_refs", [])]:
        raise BridgeError("ref_not_allowed", 403)
    if "write" in request:
        write = request["write"]
        if (not isinstance(write, dict) or set(write) != {"message", "expected_commit"}
                or not isinstance(write["message"], str) or not write["message"].strip()
                or len(write["message"]) > 500
                or not isinstance(write["expected_commit"], str)
                or not re.fullmatch(r"[0-9a-f]{40}", write["expected_commit"])):
            raise BridgeError("invalid_write_request")
        if (request["ref"] not in policy.get("write_refs", [])
                or (request["ref"] not in policy.get("write_all_refs", [])
                    and not write_prefixes_for(policy, request["ref"]))
                or re.fullmatch(r"[0-9a-f]{40}", request["ref"])):
            raise BridgeError("write_not_allowed", 403)
    program = safe_path(request["program"])
    if not program.endswith(".py") or not under(program, policy["program_prefixes"]):
        raise BridgeError("program_not_allowed", 403)
    files = request["files"]
    if not isinstance(files, list) or len(files) > MAX_FILES - 1:
        raise BridgeError("invalid_files")
    for path in files:
        if not readable(safe_path(path), policy):
            raise BridgeError("file_not_allowed", 403)
    if len(set(files)) != len(files) or not isinstance(request["input"], dict):
        raise BridgeError("invalid_request")
    return request


class GitHub:
    """fetch_json(url, headers) is injected by the hosting adapter."""
    def __init__(self, fetch_json, token):
        self.fetch_json = fetch_json
        self.headers = {"Accept": "application/vnd.github+json", "User-Agent": "agent-skill-runtime-bridge",
                        "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            self.headers["Authorization"] = "Bearer " + token

    async def get(self, path):
        return await self.fetch_json("https://api.github.com" + path, self.headers)

    async def tree(self, tree_sha):
        if not isinstance(tree_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", tree_sha):
            raise BridgeError("invalid_upstream_response", 502)
        if tree_sha not in self.trees:
            listing = await self.get(f"/repos/{self.repo}/git/trees/{tree_sha}")
            if listing.get("truncated") or not isinstance(listing.get("tree"), list):
                raise BridgeError("invalid_upstream_response", 502)
            self.trees[tree_sha] = {item["path"]: item for item in listing["tree"]}
        return self.trees[tree_sha]

    async def entry(self, path):
        directory = self.root_tree
        parts = safe_path(path).split("/")
        for index, part in enumerate(parts):
            entry = (await self.tree(directory)).get(part)
            if entry is None:
                return None
            if index < len(parts) - 1:
                if entry.get("type") != "tree" or entry.get("mode") != "040000":
                    raise BridgeError("unsupported_repository_entry", 422)
                directory = entry["sha"]
        return entry

    async def snapshot(self, request):
        repo = request["repository"]
        self.repo, self.trees = repo, {}
        ref = request["ref"]
        if re.fullmatch(r"[0-9a-f]{40}", ref):
            sha = ref
        else:
            branch = await self.get(f"/repos/{repo}/git/ref/heads/{quote(ref, safe='/')}")
            sha = branch.get("object", {}).get("sha", "")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise BridgeError("invalid_upstream_response", 502)
        if "write" in request and request["write"]["expected_commit"] != sha:
            raise BridgeError("branch_conflict", 409)
        commit = await self.get(f"/repos/{repo}/git/commits/{sha}")
        self.root_tree = commit.get("tree", {}).get("sha", "")

        files, total = {}, 0
        for path in dict.fromkeys([request["program"], *request["files"]]):
            entry = await self.entry(path)
            if entry is None:
                raise BridgeError("repository_entry_not_found", 404)
            if entry.get("type") != "blob" or entry.get("mode") not in ("100644", "100755"):
                raise BridgeError("unsupported_repository_entry", 422)
            if not isinstance(entry.get("size"), int) or not 0 <= entry["size"] <= MAX_FILE:
                raise BridgeError("file_too_large", 413)
            blob = await self.get(f"/repos/{repo}/git/blobs/{entry['sha']}")
            if blob.get("encoding") != "base64":
                raise BridgeError("invalid_upstream_response", 502)
            try:
                content = base64.b64decode("".join(blob["content"].split()), validate=True)
            except (KeyError, ValueError, TypeError):
                raise BridgeError("invalid_upstream_response", 502) from None
            if len(content) != entry["size"] or len(content) > MAX_FILE:
                raise BridgeError("invalid_upstream_response", 502)
            blob_sha = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
            if entry.get("sha") != blob_sha:
                raise BridgeError("invalid_upstream_response", 502)
            total += len(content)
            if total > MAX_TOTAL:
                raise BridgeError("snapshot_too_large", 413)
            files[path] = content
        return sha, files

    async def commit_changes(self, request, base_sha, files, changes, policy, send_json):
        if not changes:
            return {"commit": base_sha, "changed": []}
        if send_json is None:
            raise BridgeError("write_transport_unavailable", 503)
        if len(changes) > MAX_FILES:
            raise BridgeError("too_many_changes", 413)
        entries, total = [], 0
        for path, content in sorted(changes.items()):
            if not writable(safe_path(path), policy, request["ref"]):
                raise BridgeError("write_path_not_allowed", 403)
            previous = await self.entry(path)
            if previous and (previous.get("type") != "blob" or previous.get("mode") not in ("100644", "100755")):
                raise BridgeError("unsupported_repository_entry", 422)
            if previous and path not in files:
                raise BridgeError("unread_file_conflict", 409)
            if content is None:
                if previous is None:
                    raise BridgeError("invalid_delete", 422)
                item = {"path": path, "mode": previous["mode"], "type": "blob", "sha": None}
            else:
                if not isinstance(content, bytes) or len(content) > MAX_FILE:
                    raise BridgeError("file_too_large", 413)
                total += len(content)
                if total > MAX_TOTAL:
                    raise BridgeError("changes_too_large", 413)
                try:
                    text = content.decode("utf-8")
                except UnicodeError:
                    raise BridgeError("write_requires_utf8", 422) from None
                item = {"path": path, "mode": previous["mode"] if previous else "100644",
                        "type": "blob", "content": text}
            entries.append(item)
        # All changes are checked before the first write request. The final ref
        # update is fast-forward-only; a concurrent writer causes a conflict.
        ref_path = f"/repos/{self.repo}/git/ref/heads/{quote(request['ref'], safe='/')}"
        current = await self.get(ref_path)
        if current.get("object", {}).get("sha") != base_sha:
            raise BridgeError("branch_conflict", 409)

        async def send(method, suffix, body):
            return await send_json(method, f"https://api.github.com/repos/{self.repo}/git/{suffix}", self.headers, body)

        tree = await send("POST", "trees", {"base_tree": self.root_tree, "tree": entries})
        tree_sha = tree.get("sha", "")
        if not isinstance(tree_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", tree_sha):
            raise BridgeError("invalid_upstream_response", 502)
        commit = await send("POST", "commits", {"message": request["write"]["message"],
                           "parents": [base_sha], "tree": tree_sha})
        commit_sha = commit.get("sha", "")
        if not isinstance(commit_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
            raise BridgeError("invalid_upstream_response", 502)
        updated = await send("PATCH", "refs/heads/" + quote(request["ref"], safe="/"),
                             {"sha": commit_sha, "force": False})
        if updated.get("object", {}).get("sha") != commit_sha:
            raise BridgeError("invalid_upstream_response", 502)
        return {"commit": commit_sha, "changed": sorted(changes)}


async def handle(raw, authorization, settings, fetch_json, execute, send_json=None):
    try:
        request = parse_request(raw, authorization, settings)
        github = GitHub(fetch_json, settings.github_token)
        sha, files = await github.snapshot(request)
        execution = execute(files, request["program"], request["input"])
        result = execution.result
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > MAX_RESULT:
            raise BridgeError("result_too_large", 413)
        response = {"ok": True, "result": result, "source": {
            "repository": request["repository"], "ref": request["ref"], "commit": sha,
            "program": request["program"],
            "sha256": hashlib.sha256(files[request["program"]]).hexdigest()}}
        if "write" in request:
            response["write"] = await github.commit_changes(request, sha, files, execution.changes,
                                   settings.repositories[request["repository"]], send_json)
        return 200, response
    except BridgeError as error:
        return error.status, {"ok": False, "error": {"code": error.code}}
    except Exception:
        # Never return exception text, source, credentials, or private program logs.
        return 500, {"ok": False, "error": {"code": "execution_failed"}}
