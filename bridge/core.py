"""Protocol, authorization and immutable GitHub snapshot loading. No vendor SDK."""
import base64
import hashlib
import hmac
import json
import re
from urllib.parse import quote

MAX_REQUEST = 64 * 1024
MAX_FILE = 512 * 1024
MAX_TOTAL = 2 * 1024 * 1024
MAX_FILES = 32
MAX_RESULT = 512 * 1024


class BridgeError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def safe_path(value):
    if (not isinstance(value, str) or not value or len(value) > 1024
            or "\\" in value or any(ord(c) < 32 for c in value)
            or any(p in ("", ".", "..", ".git") for p in value.split("/"))):
        raise BridgeError("invalid_path")
    return value


def under(path, prefixes):
    return any(path == p or path.startswith(p + "/") for p in prefixes)


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
            for field in ("program_prefixes", "data_prefixes"):
                prefixes = policy.get(field)
                if not isinstance(prefixes, list) or not prefixes:
                    raise BridgeError("server_not_configured", 503)
                for prefix in prefixes:
                    safe_path(prefix)
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
    if not isinstance(request, dict) or set(request) != fields:
        raise BridgeError("invalid_request")
    repo = request["repository"]
    if not isinstance(repo, str) or repo not in settings.repositories:
        raise BridgeError("repository_not_allowed", 403)
    policy = settings.repositories[repo]
    if request["ref"] != policy["ref"]:
        raise BridgeError("ref_not_allowed", 403)
    program = safe_path(request["program"])
    if not program.endswith(".py") or not under(program, policy["program_prefixes"]):
        raise BridgeError("program_not_allowed", 403)
    files = request["files"]
    if not isinstance(files, list) or len(files) > MAX_FILES - 1:
        raise BridgeError("invalid_files")
    for path in files:
        if not under(safe_path(path), policy["data_prefixes"] + policy["program_prefixes"]):
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

    async def snapshot(self, request):
        repo = request["repository"]
        ref = request["ref"]
        if re.fullmatch(r"[0-9a-f]{40}", ref):
            sha = ref
        else:
            branch = await self.get(f"/repos/{repo}/git/ref/heads/{quote(ref, safe='/')}")
            sha = branch.get("object", {}).get("sha", "")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise BridgeError("invalid_upstream_response", 502)
        commit = await self.get(f"/repos/{repo}/git/commits/{sha}")
        root_tree = commit.get("tree", {}).get("sha", "")
        trees = {}

        async def tree(tree_sha):
            if not isinstance(tree_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", tree_sha):
                raise BridgeError("invalid_upstream_response", 502)
            if tree_sha not in trees:
                listing = await self.get(f"/repos/{repo}/git/trees/{tree_sha}")
                if listing.get("truncated") or not isinstance(listing.get("tree"), list):
                    raise BridgeError("invalid_upstream_response", 502)
                trees[tree_sha] = {item["path"]: item for item in listing["tree"]}
            return trees[tree_sha]

        files, total = {}, 0
        for path in dict.fromkeys([request["program"], *request["files"]]):
            directory = root_tree
            parts = path.split("/")
            for index, part in enumerate(parts):
                entry = (await tree(directory)).get(part)
                if entry is None:
                    raise BridgeError("repository_entry_not_found", 404)
                if index < len(parts) - 1:
                    if entry.get("type") != "tree" or entry.get("mode") != "040000":
                        raise BridgeError("unsupported_repository_entry", 422)
                    directory = entry["sha"]
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


async def handle(raw, authorization, settings, fetch_json, execute):
    try:
        request = parse_request(raw, authorization, settings)
        sha, files = await GitHub(fetch_json, settings.github_token).snapshot(request)
        result = execute(files, request["program"], request["input"])
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > MAX_RESULT:
            raise BridgeError("result_too_large", 413)
        return 200, {"ok": True, "result": result, "source": {
            "repository": request["repository"], "ref": request["ref"], "commit": sha,
            "program": request["program"],
            "sha256": hashlib.sha256(files[request["program"]]).hexdigest()}}
    except BridgeError as error:
        return error.status, {"ok": False, "error": {"code": error.code}}
    except Exception:
        # Never return exception text, source, credentials, or private program logs.
        return 500, {"ok": False, "error": {"code": "execution_failed"}}
