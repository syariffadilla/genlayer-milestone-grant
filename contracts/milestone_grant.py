# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
MilestoneGrant - milestone-based grant / bounty escrow released on public GitHub state.

A funder creates a grant for one grantee address and one public GitHub
repository (owner/repo, fixed at creation) with 1..20 milestones. Each
milestone has a GEN amount, a deadline and objective, machine-checkable
criteria:

  (a) a GitHub release with an exact tag exists and was published at or
      before the deadline (and optionally at or after `not_before`);
  (b) optionally the commit the tag points to has all check runs passing
      (or a named list of required check runs), and/or a combined commit
      status of `success`;
  (c) optionally a given pull request is merged into a given base branch
      at or before the deadline;
  (d) optionally at least N commits by a given GitHub login in a fixed
      time range.

The funder deposits the full sum at creation (payable, exact amount).
The grantee claims a milestone; then anyone may call verify_milestone().
Verification reads GitHub's public REST API (unauthenticated) inside a
custom leader/validator block (gl.vm.run_nondet): each validator re-fetches
GitHub itself and requires every settlement-driving field of the leader's
evidence to be identical to its own. No LLM is used.

Outcomes:  PASS  -> the milestone amount is sent to the grantee (once).
           FAIL  -> no money moves; milestone re-opens (grantee may re-claim
                    before the deadline).
           DEFER -> no money moves (GitHub error / 404 / rate limit /
                    checks still running); milestone stays CLAIMED, retryable.
After deadline + grace, any milestone that is not PAID can be reclaimed by
the funder. The funder can cancel a milestone that was never claimed while
its deadline is still in the future.

Units:
  * money: wei of native GEN (int / u256), integers only
  * time: unix seconds (UTC) as int
"""

import json
import datetime

from dataclasses import dataclass

from genlayer import *


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

API = "https://api.github.com"
HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "genlayer-milestone-grant",
}

MAX_MILESTONES = 20
MAX_REQUIRED_CHECKS = 20
MAX_MIN_COMMITS = 100  # commits are counted from one page of per_page=100
MIN_GRACE_DAYS = 1
MAX_GRACE_DAYS = 90
MAX_DEADLINE_AHEAD_S = 3 * 366 * 86400
DAY_S = 86400

CHECKS_NONE = "none"
CHECKS_RUNS = "check_runs"
CHECKS_STATUS = "status"
CHECKS_BOTH = "both"
CHECK_MODES = [CHECKS_NONE, CHECKS_RUNS, CHECKS_STATUS, CHECKS_BOTH]

# check-run conclusions that count as passing
PASSING_CONCLUSIONS = ["success", "neutral", "skipped"]

M_OPEN = "OPEN"
M_CLAIMED = "CLAIMED"
M_PAID = "PAID"
M_RECLAIMED = "RECLAIMED"
M_CANCELLED = "CANCELLED"

PASS = "PASS"
FAIL = "FAIL"
DEFER = "DEFER"
OK = "OK"

SPEC_KEYS = [
    "amount",
    "deadline",
    "tag",
    "checks",
    "required_checks",
    "not_before",
    "pr_number",
    "pr_base",
    "commit_author",
    "min_commits",
    "commits_since",
    "commits_until",
]

# Every key of the evidence dict. Validators require all of them to be identical.
EVIDENCE_KEYS = [
    "grant_id",
    "milestone",
    "owner",
    "repo",
    "tag",
    "deadline",
    "fetch_error",
    "release_http",
    "release_tag",
    "release_draft",
    "release_published_at",
    "release_published_ts",
    "commit_http",
    "commit_sha",
    "checks_mode",
    "check_runs_http",
    "check_runs_total",
    "check_runs",
    "status_http",
    "status_state",
    "status_total",
    "statuses",
    "pr_number",
    "pr_http",
    "pr_state",
    "pr_merged",
    "pr_merged_at",
    "pr_merged_ts",
    "pr_base",
    "commit_author",
    "commits_http",
    "commits_count",
    "verdict",
    "reason",
    "payout",
]

IDENTITY_KEYS = ["grant_id", "milestone", "owner", "repo", "tag", "deadline", "checks_mode", "pr_number", "commit_author"]

EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)

NAME_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
LOGIN_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-"


# --------------------------------------------------------------------------
# Pure helpers (deterministic; shared by leader, validator and contract)
# --------------------------------------------------------------------------


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _check_name(v, what: str, chars: str, max_len: int) -> str:
    if not isinstance(v, str) or len(v) == 0 or len(v) > max_len:
        raise gl.vm.UserError("Invalid " + what)
    for ch in v:
        if ch not in chars:
            raise gl.vm.UserError("Invalid " + what)
    if v in [".", ".."] or ".." in v:
        raise gl.vm.UserError("Invalid " + what)
    return v


def _ts_to_iso(ts: int) -> str:
    return (EPOCH + datetime.timedelta(seconds=ts)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_to_ts(s) -> int:
    """Strict 'YYYY-MM-DDTHH:MM:SSZ' (GitHub format) -> unix seconds. -1 if invalid."""
    if not isinstance(s, str) or len(s) != 20 or s[10] != "T" or s[19] != "Z":
        return -1
    try:
        d = datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return -1
    if d.strftime("%Y-%m-%dT%H:%M:%SZ") != s:
        return -1
    return (d - EPOCH) // datetime.timedelta(seconds=1)


def _repo_base(owner: str, repo: str) -> str:
    return API + "/repos/" + owner + "/" + repo


def _url_release(owner: str, repo: str, tag: str) -> str:
    return _repo_base(owner, repo) + "/releases/tags/" + tag


def _url_commit(owner: str, repo: str, tag: str) -> str:
    return _repo_base(owner, repo) + "/commits/refs/tags/" + tag


def _url_check_runs(owner: str, repo: str, sha: str) -> str:
    return _repo_base(owner, repo) + "/commits/" + sha + "/check-runs?filter=latest&per_page=100"


def _url_status(owner: str, repo: str, sha: str) -> str:
    return _repo_base(owner, repo) + "/commits/" + sha + "/status?per_page=100"


def _url_pull(owner: str, repo: str, number: int) -> str:
    return _repo_base(owner, repo) + "/pulls/" + str(number)


def _url_commits(owner: str, repo: str, author: str, since_ts: int, until_ts: int) -> str:
    return (
        _repo_base(owner, repo)
        + "/commits?author=" + author
        + "&since=" + _ts_to_iso(since_ts)
        + "&until=" + _ts_to_iso(until_ts)
        + "&per_page=100"
    )


def _blank_evidence(grant_id: str, index: int, c: dict) -> dict:
    return {
        "grant_id": grant_id,
        "milestone": index,
        "owner": c["owner"],
        "repo": c["repo"],
        "tag": c["tag"],
        "deadline": c["deadline"],
        "fetch_error": "",
        "release_http": 0,
        "release_tag": "",
        "release_draft": False,
        "release_published_at": "",
        "release_published_ts": -1,
        "commit_http": 0,
        "commit_sha": "",
        "checks_mode": c["checks"],
        "check_runs_http": 0,
        "check_runs_total": -1,
        "check_runs": [],
        "status_http": 0,
        "status_state": "",
        "status_total": -1,
        "statuses": [],
        "pr_number": c["pr_number"],
        "pr_http": 0,
        "pr_state": "",
        "pr_merged": False,
        "pr_merged_at": "",
        "pr_merged_ts": -1,
        "pr_base": "",
        "commit_author": c["commit_author"],
        "commits_http": 0,
        "commits_count": -1,
        "verdict": "",
        "reason": "",
        "payout": 0,
    }


def _http_error(status: int, body, endpoint: str) -> str:
    """Deterministic classification of a non-200 GitHub response."""
    if status == 429:
        return "GITHUB_RATE_LIMIT:" + endpoint
    if status == 403:
        text = ""
        if body is not None:
            try:
                text = body.decode("utf-8").lower()
            except Exception:
                text = ""
        if "rate limit" in text:
            return "GITHUB_RATE_LIMIT:" + endpoint
        return "GITHUB_HTTP_403:" + endpoint
    if status == 404:
        return "GITHUB_NOT_FOUND:" + endpoint
    return "GITHUB_HTTP_" + str(status) + ":" + endpoint


def _get_json(url: str, endpoint: str) -> list:
    """Returns [http_status, parsed_json_or_None, error_reason]."""
    try:
        r = gl.nondet.web.get(url, headers=HEADERS)
    except Exception:
        return [0, None, "GITHUB_UNREACHABLE:" + endpoint]
    status = int(r.status)
    if status != 200:
        return [status, None, _http_error(status, r.body, endpoint)]
    try:
        data = json.loads(r.body.decode("utf-8"))
    except Exception:
        return [status, None, "GITHUB_BAD_RESPONSE:" + endpoint]
    return [status, data, ""]


def _str(v) -> str:
    return v if isinstance(v, str) else ""


def _parse_release(ev: dict, data) -> str:
    if not isinstance(data, dict) or not isinstance(data.get("tag_name"), str):
        return "GITHUB_BAD_RESPONSE:release"
    ev["release_tag"] = data["tag_name"]
    ev["release_draft"] = data.get("draft") is True
    ev["release_published_at"] = _str(data.get("published_at"))
    ev["release_published_ts"] = _iso_to_ts(data.get("published_at"))
    return ""


def _parse_commit(ev: dict, data) -> str:
    sha = data.get("sha") if isinstance(data, dict) else None
    if not isinstance(sha, str) or len(sha) != 40:
        return "GITHUB_BAD_RESPONSE:commit"
    for ch in sha:
        if ch not in "0123456789abcdef":
            return "GITHUB_BAD_RESPONSE:commit"
    ev["commit_sha"] = sha
    return ""


def _parse_check_runs(ev: dict, data) -> str:
    if not isinstance(data, dict) or not _is_int(data.get("total_count")) or not isinstance(data.get("check_runs"), list):
        return "GITHUB_BAD_RESPONSE:check_runs"
    runs = []
    for run in data["check_runs"]:
        if not isinstance(run, dict) or not isinstance(run.get("name"), str):
            return "GITHUB_BAD_RESPONSE:check_runs"
        runs.append([run["name"], _str(run.get("status")), _str(run.get("conclusion"))])
    runs.sort()
    ev["check_runs_total"] = data["total_count"]
    ev["check_runs"] = runs
    return ""


def _parse_status(ev: dict, data) -> str:
    if not isinstance(data, dict) or not isinstance(data.get("state"), str) or not isinstance(data.get("statuses"), list):
        return "GITHUB_BAD_RESPONSE:status"
    if not _is_int(data.get("total_count")):
        return "GITHUB_BAD_RESPONSE:status"
    sts = []
    for s in data["statuses"]:
        if not isinstance(s, dict) or not isinstance(s.get("context"), str):
            return "GITHUB_BAD_RESPONSE:status"
        sts.append([s["context"], _str(s.get("state"))])
    sts.sort()
    ev["status_state"] = data["state"]
    ev["status_total"] = data["total_count"]
    ev["statuses"] = sts
    return ""


def _parse_pull(ev: dict, data) -> str:
    if not isinstance(data, dict) or not _is_int(data.get("number")):
        return "GITHUB_BAD_RESPONSE:pull"
    base = data.get("base")
    ev["pr_state"] = _str(data.get("state"))
    ev["pr_merged"] = data.get("merged") is True
    ev["pr_merged_at"] = _str(data.get("merged_at"))
    ev["pr_merged_ts"] = _iso_to_ts(data.get("merged_at"))
    ev["pr_base"] = _str(base.get("ref")) if isinstance(base, dict) else ""
    if data["number"] != ev["pr_number"]:
        return "GITHUB_BAD_RESPONSE:pull"
    return ""


def _parse_commits(ev: dict, data) -> str:
    if not isinstance(data, list):
        return "GITHUB_BAD_RESPONSE:commits"
    ev["commits_count"] = len(data)
    return ""


def _evaluate(ev: dict, c: dict) -> list:
    """Deterministic verdict from the evidence facts and the milestone criteria.

    Returns [verdict, reason, payout]. Any fetch error -> DEFER with that reason.
    Otherwise every enabled criterion is evaluated; the first FAIL wins, else
    the first DEFER, else PASS (payout = milestone amount).
    """
    if ev["fetch_error"] != "":
        return [DEFER, ev["fetch_error"], 0]
    results = []

    # (a) release
    if ev["release_tag"] != c["tag"]:
        results.append([FAIL, "TAG_MISMATCH"])
    elif ev["release_draft"]:
        results.append([FAIL, "RELEASE_IS_DRAFT"])
    elif ev["release_published_ts"] < 0:
        results.append([DEFER, "RELEASE_NOT_PUBLISHED"])
    elif ev["release_published_ts"] > c["deadline"]:
        results.append([FAIL, "RELEASE_AFTER_DEADLINE"])
    elif ev["release_published_ts"] < c["not_before"]:
        results.append([FAIL, "RELEASE_BEFORE_NOT_BEFORE"])
    else:
        results.append([OK, ""])

    # (b1) check runs
    if c["checks"] in [CHECKS_RUNS, CHECKS_BOTH]:
        cr = []
        runs = ev["check_runs"]
        if ev["check_runs_total"] != len(runs):
            cr.append([DEFER, "CHECKS_TRUNCATED"])
        else:
            req = c["required_checks"]
            considered = []
            if len(req) == 0:
                considered = runs
                if len(runs) == 0:
                    cr.append([FAIL, "CHECKS_NONE"])
            else:
                for name in req:
                    named = [r for r in runs if r[0] == name]
                    if len(named) == 0:
                        cr.append([FAIL, "REQUIRED_CHECK_MISSING:" + name])
                    considered = considered + named
            for r in considered:
                if r[1] != "completed":
                    cr.append([DEFER, "CHECK_PENDING:" + r[0]])
                elif r[2] not in PASSING_CONCLUSIONS:
                    cr.append([FAIL, "CHECK_FAILED:" + r[0]])
        if len(cr) == 0:
            cr.append([OK, ""])
        results = results + cr

    # (b2) combined status
    if c["checks"] in [CHECKS_STATUS, CHECKS_BOTH]:
        if ev["status_total"] == 0:
            results.append([FAIL, "STATUS_NONE"])
        elif ev["status_state"] == "success":
            results.append([OK, ""])
        elif ev["status_state"] == "pending":
            results.append([DEFER, "STATUS_PENDING"])
        else:
            results.append([FAIL, "STATUS_" + ev["status_state"].upper()])

    # (c) pull request
    if c["pr_number"] > 0:
        if not ev["pr_merged"] or ev["pr_merged_ts"] < 0:
            results.append([FAIL, "PR_NOT_MERGED"])
        elif ev["pr_base"] != c["pr_base"]:
            results.append([FAIL, "PR_WRONG_BASE"])
        elif ev["pr_merged_ts"] > c["deadline"]:
            results.append([FAIL, "PR_MERGED_AFTER_DEADLINE"])
        else:
            results.append([OK, ""])

    # (d) commit count
    if c["commit_author"] != "":
        if ev["commits_count"] < c["min_commits"]:
            results.append([FAIL, "TOO_FEW_COMMITS"])
        else:
            results.append([OK, ""])

    for r in results:
        if r[0] == FAIL:
            return [FAIL, r[1], 0]
    for r in results:
        if r[0] == DEFER:
            return [DEFER, r[1], 0]
    return [PASS, "", c["amount"]]


def _fetch_evidence(grant_id: str, index: int, c: dict) -> dict:
    """Non-deterministic: query GitHub and build the settlement evidence.
    Stops at the first failed request (saves rate limit; error -> DEFER)."""
    ev = _blank_evidence(grant_id, index, c)
    o = c["owner"]
    rp = c["repo"]

    def step(http_key: str, url: str, endpoint: str, parser) -> bool:
        st, data, err = _get_json(url, endpoint)
        ev[http_key] = st
        if err == "":
            err = parser(ev, data)
        if err != "":
            ev["fetch_error"] = err
            return False
        return True

    ok = step("release_http", _url_release(o, rp, c["tag"]), "release", _parse_release)
    ok = ok and step("commit_http", _url_commit(o, rp, c["tag"]), "commit", _parse_commit)
    if ok and c["checks"] in [CHECKS_RUNS, CHECKS_BOTH]:
        ok = step("check_runs_http", _url_check_runs(o, rp, ev["commit_sha"]), "check_runs", _parse_check_runs)
    if ok and c["checks"] in [CHECKS_STATUS, CHECKS_BOTH]:
        ok = step("status_http", _url_status(o, rp, ev["commit_sha"]), "status", _parse_status)
    if ok and c["pr_number"] > 0:
        ok = step("pr_http", _url_pull(o, rp, c["pr_number"]), "pull", _parse_pull)
    if ok and c["commit_author"] != "":
        ok = step(
            "commits_http",
            _url_commits(o, rp, c["commit_author"], c["commits_since"], c["commits_until"]),
            "commits",
            _parse_commits,
        )
    verdict = _evaluate(ev, c)
    ev["verdict"] = verdict[0]
    ev["reason"] = verdict[1]
    ev["payout"] = verdict[2]
    return ev


def _canonical(v) -> str:
    # json distinguishes true/1 and "1"/1, so this is a strict, typed equality
    return json.dumps(v, sort_keys=True, separators=(",", ":"))


def _evidence_matches(leader, mine: dict, c: dict) -> bool:
    """Validator equivalence: every key of the evidence must be identical to the
    validator's own independent fetch, and the leader's verdict must follow
    from the leader's own facts."""
    if not isinstance(leader, dict):
        return False
    if sorted(leader.keys()) != sorted(EVIDENCE_KEYS):
        return False
    if _canonical(leader) != _canonical(mine):
        return False
    recomputed = _evaluate(leader, c)
    return [leader["verdict"], leader["reason"], leader["payout"]] == recomputed


# --------------------------------------------------------------------------
# Storage types
# --------------------------------------------------------------------------


@allow_storage
@dataclass
class Grant:
    id: str
    funder: Address
    grantee: Address
    owner: str
    repo: str
    grace_seconds: u256
    created_at: u256
    milestone_count: u32
    deposited: u256
    paid: u256
    refunded: u256


@allow_storage
@dataclass
class Milestone:
    amount: u256
    deadline: u256
    not_before: u256
    tag: str
    checks: str
    required_checks_json: str
    pr_number: u256
    pr_base: str
    commit_author: str
    min_commits: u32
    commits_since: u256
    commits_until: u256
    status: str
    claims: u32
    verify_attempts: u32
    paid_amount: u256
    last_verdict: str
    last_reason: str
    evidence: str


@gl.evm.contract_interface
class _Recipient:
    class View:
        pass

    class Write:
        pass


# --------------------------------------------------------------------------
# Contract
# --------------------------------------------------------------------------


class MilestoneGrant(gl.Contract):
    grant_count: u256
    total_deposited: u256
    total_paid: u256
    total_refunded: u256
    grants: TreeMap[str, Grant]
    milestones: TreeMap[str, Milestone]

    def __init__(self):
        self.grant_count = u256(0)
        self.total_deposited = u256(0)
        self.total_paid = u256(0)
        self.total_refunded = u256(0)

    # ---------------- internal helpers ----------------

    def _now(self) -> int:
        now = datetime.datetime.now(datetime.timezone.utc)
        return (now - EPOCH) // datetime.timedelta(seconds=1)

    def _send(self, to: Address, amount: int) -> None:
        if amount > 0:
            _Recipient(to).emit_transfer(value=u256(amount))

    def _grant(self, grant_id: str) -> Grant:
        if not isinstance(grant_id, str) or grant_id not in self.grants:
            raise gl.vm.UserError("Unknown grant")
        return self.grants[grant_id]

    def _key(self, grant_id: str, index: int) -> str:
        g = self._grant(grant_id)
        if not _is_int(index) or index < 0 or index >= int(g.milestone_count):
            raise gl.vm.UserError("Unknown milestone")
        return grant_id + "/" + str(index)

    def _criteria(self, g: Grant, m: Milestone) -> dict:
        return {
            "owner": str(g.owner),
            "repo": str(g.repo),
            "tag": str(m.tag),
            "deadline": int(m.deadline),
            "not_before": int(m.not_before),
            "checks": str(m.checks),
            "required_checks": json.loads(m.required_checks_json),
            "pr_number": int(m.pr_number),
            "pr_base": str(m.pr_base),
            "commit_author": str(m.commit_author),
            "min_commits": int(m.min_commits),
            "commits_since": int(m.commits_since),
            "commits_until": int(m.commits_until),
            "amount": int(m.amount),
        }

    def _validate_spec(self, spec, now: int) -> dict:
        if not isinstance(spec, dict):
            raise gl.vm.UserError("Milestone must be an object")
        for k in spec.keys():
            if k not in SPEC_KEYS:
                raise gl.vm.UserError("Unknown milestone field: " + str(k))
        amount = spec.get("amount")
        if not _is_int(amount) or amount <= 0:
            raise gl.vm.UserError("Invalid amount")
        deadline = spec.get("deadline")
        if not _is_int(deadline) or deadline <= now:
            raise gl.vm.UserError("Deadline must be in the future")
        if deadline > now + MAX_DEADLINE_AHEAD_S:
            raise gl.vm.UserError("Deadline too far ahead")
        not_before = spec.get("not_before", 0)
        if not _is_int(not_before) or not_before < 0 or not_before > deadline:
            raise gl.vm.UserError("Invalid not_before")
        tag = _check_name(spec.get("tag"), "tag", NAME_CHARS, 100)
        checks = spec.get("checks", CHECKS_NONE)
        if checks not in CHECK_MODES:
            raise gl.vm.UserError("Invalid checks mode")
        req = spec.get("required_checks", [])
        if not isinstance(req, list) or len(req) > MAX_REQUIRED_CHECKS:
            raise gl.vm.UserError("Invalid required_checks")
        for name in req:
            if not isinstance(name, str) or len(name) == 0 or len(name) > 100:
                raise gl.vm.UserError("Invalid required_checks")
        if len(req) != len(set(req)):
            raise gl.vm.UserError("Duplicate required check")
        if len(req) > 0 and checks not in [CHECKS_RUNS, CHECKS_BOTH]:
            raise gl.vm.UserError("required_checks need checks mode check_runs or both")
        pr_number = spec.get("pr_number", 0)
        if not _is_int(pr_number) or pr_number < 0:
            raise gl.vm.UserError("Invalid pr_number")
        pr_base = spec.get("pr_base", "")
        if pr_number > 0:
            pr_base = _check_name(pr_base, "pr_base", NAME_CHARS + "/", 100)
        elif pr_base != "":
            raise gl.vm.UserError("pr_base given without pr_number")
        author = spec.get("commit_author", "")
        min_commits = spec.get("min_commits", 0)
        since = spec.get("commits_since", 0)
        until = spec.get("commits_until", 0)
        if author != "":
            _check_name(author, "commit_author", LOGIN_CHARS, 39)
            if not _is_int(min_commits) or min_commits < 1 or min_commits > MAX_MIN_COMMITS:
                raise gl.vm.UserError("Invalid min_commits")
            if not _is_int(since) or not _is_int(until) or since < 0 or until <= since:
                raise gl.vm.UserError("Invalid commit range")
            if until > deadline:
                raise gl.vm.UserError("Commit range must end by the deadline")
        elif min_commits != 0 or since != 0 or until != 0:
            raise gl.vm.UserError("Commit criteria given without commit_author")
        return {
            "amount": amount,
            "deadline": deadline,
            "not_before": not_before,
            "tag": tag,
            "checks": checks,
            "required_checks": req,
            "pr_number": pr_number,
            "pr_base": pr_base,
            "commit_author": author,
            "min_commits": min_commits,
            "commits_since": since,
            "commits_until": until,
        }

    # ---------------- funder ----------------

    @gl.public.write.payable
    def create_grant(self, grantee: str, owner: str, repo: str, grace_days: int, milestones: list) -> str:
        try:
            grantee_addr = Address(grantee)
        except Exception:
            raise gl.vm.UserError("Invalid grantee address")
        if grantee_addr == Address(b"\x00" * 20):
            raise gl.vm.UserError("Invalid grantee address")
        funder = gl.message.sender_address
        if grantee_addr == funder:
            raise gl.vm.UserError("Grantee must differ from funder")
        _check_name(owner, "owner", NAME_CHARS, 100)
        _check_name(repo, "repo", NAME_CHARS, 100)
        if not _is_int(grace_days) or grace_days < MIN_GRACE_DAYS or grace_days > MAX_GRACE_DAYS:
            raise gl.vm.UserError("Invalid grace_days")
        if not isinstance(milestones, list) or len(milestones) == 0 or len(milestones) > MAX_MILESTONES:
            raise gl.vm.UserError("Need 1-20 milestones")
        now = self._now()
        specs = [self._validate_spec(s, now) for s in milestones]
        total = 0
        for s in specs:
            total += s["amount"]
        value = int(gl.message.value)
        if value < total:
            raise gl.vm.UserError("Insufficient deposit: send exactly " + str(total))
        if value > total:
            raise gl.vm.UserError("Deposit exceeds milestone total: send exactly " + str(total))

        new_count = int(self.grant_count) + 1
        grant_id = str(new_count)
        self.grant_count = u256(new_count)
        self.grants[grant_id] = Grant(
            id=grant_id,
            funder=funder,
            grantee=grantee_addr,
            owner=owner,
            repo=repo,
            grace_seconds=u256(grace_days * DAY_S),
            created_at=u256(now),
            milestone_count=u32(len(specs)),
            deposited=u256(total),
            paid=u256(0),
            refunded=u256(0),
        )
        for i in range(len(specs)):
            s = specs[i]
            self.milestones[grant_id + "/" + str(i)] = Milestone(
                amount=u256(s["amount"]),
                deadline=u256(s["deadline"]),
                not_before=u256(s["not_before"]),
                tag=s["tag"],
                checks=s["checks"],
                required_checks_json=json.dumps(s["required_checks"]),
                pr_number=u256(s["pr_number"]),
                pr_base=s["pr_base"],
                commit_author=s["commit_author"],
                min_commits=u32(s["min_commits"]),
                commits_since=u256(s["commits_since"]),
                commits_until=u256(s["commits_until"]),
                status=M_OPEN,
                claims=u32(0),
                verify_attempts=u32(0),
                paid_amount=u256(0),
                last_verdict="",
                last_reason="",
                evidence="",
            )
        self.total_deposited = u256(int(self.total_deposited) + total)
        return grant_id

    @gl.public.write
    def cancel_milestone(self, grant_id: str, index: int) -> int:
        """Funder cancels a milestone that was never claimed, before its deadline."""
        key = self._key(grant_id, index)
        g = self.grants[grant_id]
        m = self.milestones[key]
        if gl.message.sender_address != g.funder:
            raise gl.vm.UserError("Only funder")
        if m.status != M_OPEN or int(m.claims) != 0:
            raise gl.vm.UserError("Only never-claimed open milestones can be cancelled")
        if self._now() >= int(m.deadline):
            raise gl.vm.UserError("Deadline passed; use reclaim_milestone")
        amount = int(m.amount)
        m.status = M_CANCELLED
        g.refunded = u256(int(g.refunded) + amount)
        self.total_refunded = u256(int(self.total_refunded) + amount)
        self._send(g.funder, amount)
        return amount

    @gl.public.write
    def reclaim_milestone(self, grant_id: str, index: int) -> int:
        """Funder takes back a milestone that did not pass by deadline + grace."""
        key = self._key(grant_id, index)
        g = self.grants[grant_id]
        m = self.milestones[key]
        if gl.message.sender_address != g.funder:
            raise gl.vm.UserError("Only funder")
        if m.status not in [M_OPEN, M_CLAIMED]:
            raise gl.vm.UserError("Milestone not reclaimable in status " + m.status)
        if self._now() <= int(m.deadline) + int(g.grace_seconds):
            raise gl.vm.UserError("Deadline + grace not passed yet")
        amount = int(m.amount)
        m.status = M_RECLAIMED
        g.refunded = u256(int(g.refunded) + amount)
        self.total_refunded = u256(int(self.total_refunded) + amount)
        self._send(g.funder, amount)
        return amount

    # ---------------- grantee ----------------

    @gl.public.write
    def claim_milestone(self, grant_id: str, index: int) -> None:
        key = self._key(grant_id, index)
        g = self.grants[grant_id]
        m = self.milestones[key]
        if gl.message.sender_address != g.grantee:
            raise gl.vm.UserError("Only grantee")
        if m.status != M_OPEN:
            raise gl.vm.UserError("Milestone not open (status " + m.status + ")")
        if self._now() > int(m.deadline):
            raise gl.vm.UserError("Deadline passed")
        m.status = M_CLAIMED
        m.claims = u32(int(m.claims) + 1)

    # ---------------- verification ----------------

    @gl.public.write
    def verify_milestone(self, grant_id: str, index: int) -> dict:
        key = self._key(grant_id, index)
        g = self.grants[grant_id]
        m = self.milestones[key]
        if m.status != M_CLAIMED:
            raise gl.vm.UserError("Milestone not claimed (status " + m.status + ")")
        if self._now() > int(m.deadline) + int(g.grace_seconds):
            raise gl.vm.UserError("Verification window closed")

        # plain locals only inside the nondet block
        gid = str(grant_id)
        idx = int(index)
        crit = self._criteria(g, m)

        def leader_fn() -> dict:
            return _fetch_evidence(gid, idx, crit)

        def validator_fn(leaders_res) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return False
            mine = _fetch_evidence(gid, idx, crit)
            return _evidence_matches(leaders_res.calldata, mine, crit)

        ev = gl.vm.run_nondet(leader_fn, validator_fn)

        # ---- deterministic post-consensus checks and integer money logic ----
        if not isinstance(ev, dict) or sorted(ev.keys()) != sorted(EVIDENCE_KEYS):
            raise gl.vm.UserError("Malformed evidence")
        expected = _blank_evidence(gid, idx, crit)
        for k in IDENTITY_KEYS:
            if _canonical(ev[k]) != _canonical(expected[k]):
                raise gl.vm.UserError("Evidence does not match milestone")
        verdict, reason, payout = _evaluate(ev, crit)
        if ev["verdict"] != verdict or ev["reason"] != reason or ev["payout"] != payout:
            raise gl.vm.UserError("Evidence verdict inconsistent")

        m.verify_attempts = u32(int(m.verify_attempts) + 1)
        m.last_verdict = verdict
        m.last_reason = reason
        m.evidence = _canonical(ev)
        if verdict == PASS:
            amount = int(m.amount)
            if payout != amount or int(m.paid_amount) != 0:
                raise gl.vm.UserError("Payout mismatch")
            m.status = M_PAID
            m.paid_amount = u256(amount)
            g.paid = u256(int(g.paid) + amount)
            self.total_paid = u256(int(self.total_paid) + amount)
            self._send(g.grantee, amount)
        elif verdict == FAIL:
            m.status = M_OPEN  # grantee may fix and re-claim before the deadline
        # DEFER: stays CLAIMED, retryable, no money moves
        return ev

    # ---------------- views ----------------

    def _milestone_view(self, grant_id: str, index: int) -> dict:
        g = self.grants[grant_id]
        m = self.milestones[grant_id + "/" + str(index)]
        return {
            "index": index,
            "amount": int(m.amount),
            "deadline": int(m.deadline),
            "deadline_iso": _ts_to_iso(int(m.deadline)),
            "reclaimable_after": int(m.deadline) + int(g.grace_seconds),
            "not_before": int(m.not_before),
            "tag": m.tag,
            "checks": m.checks,
            "required_checks": json.loads(m.required_checks_json),
            "pr_number": int(m.pr_number),
            "pr_base": m.pr_base,
            "commit_author": m.commit_author,
            "min_commits": int(m.min_commits),
            "commits_since": int(m.commits_since),
            "commits_until": int(m.commits_until),
            "status": m.status,
            "claims": int(m.claims),
            "verify_attempts": int(m.verify_attempts),
            "paid_amount": int(m.paid_amount),
            "last_verdict": m.last_verdict,
            "last_reason": m.last_reason,
            "evidence": m.evidence,
        }

    @gl.public.view
    def get_grant(self, grant_id: str) -> dict:
        g = self._grant(grant_id)
        locked = 0
        ms = []
        for i in range(int(g.milestone_count)):
            v = self._milestone_view(grant_id, i)
            if v["status"] in [M_OPEN, M_CLAIMED]:
                locked += v["amount"]
            ms.append(v)
        return {
            "id": g.id,
            "funder": g.funder.as_hex,
            "grantee": g.grantee.as_hex,
            "owner": g.owner,
            "repo": g.repo,
            "grace_seconds": int(g.grace_seconds),
            "created_at": int(g.created_at),
            "deposited": int(g.deposited),
            "paid": int(g.paid),
            "refunded": int(g.refunded),
            "locked": locked,
            "milestones": ms,
        }

    @gl.public.view
    def get_milestone(self, grant_id: str, index: int) -> dict:
        self._key(grant_id, index)
        return self._milestone_view(grant_id, index)

    @gl.public.view
    def get_totals(self) -> dict:
        return {
            "grant_count": int(self.grant_count),
            "total_deposited": int(self.total_deposited),
            "total_paid": int(self.total_paid),
            "total_refunded": int(self.total_refunded),
            "total_locked": int(self.total_deposited) - int(self.total_paid) - int(self.total_refunded),
        }

    @gl.public.view
    def get_static_urls(self, grant_id: str, index: int) -> list:
        """GitHub URLs that do not depend on the resolved commit SHA."""
        key = self._key(grant_id, index)
        g = self.grants[grant_id]
        m = self.milestones[key]
        urls = [_url_release(g.owner, g.repo, m.tag), _url_commit(g.owner, g.repo, m.tag)]
        if int(m.pr_number) > 0:
            urls.append(_url_pull(g.owner, g.repo, int(m.pr_number)))
        if m.commit_author != "":
            urls.append(_url_commits(g.owner, g.repo, m.commit_author, int(m.commits_since), int(m.commits_until)))
        return urls
