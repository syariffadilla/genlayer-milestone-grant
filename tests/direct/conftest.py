"""Shared helpers for direct-mode tests of MilestoneGrant.

GitHub responses are REAL payloads captured with curl from the public,
unauthenticated GitHub REST API (see tests/direct/fixtures/README.md).
Tests that need a failing / pending / truncated variant load a real fixture
and change one field in memory; each such test says what it changed.
"""

import copy
import json
import os
import re
import sys
from pathlib import Path

# Pin the GenVM release used by gltest direct mode. Without this, gltest asks
# GitHub for the "latest" GenVM release, which is now a v0.3 release candidate
# that ships no genvm-universal tarball (HTTP 404), and every test errors on a
# fresh machine. The contract's py-genlayer hash belongs to the v0.2 line.
from gltest.direct import sdk_loader as _sdk_loader

GENVM_VERSION = os.environ.get("GLTEST_GENVM_VERSION", "v0.2.16")
_sdk_loader.get_latest_version = lambda: GENVM_VERSION

import pytest  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
CONTRACT = str(Path(__file__).resolve().parents[2] / "contracts" / "milestone_grant.py")

GEN = 10**18
OWNER = "pallets"
REPO = "flask"
TAG = "3.1.3"

# 2026-01-01T00:00:00Z: tests start before the 3.1.3 release (2026-02-19T05:01:30Z)
T0 = "2026-01-01T00:00:00Z"
DAY = 86400


def ts(iso: str) -> int:
    import datetime

    d = datetime.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    return int(d.timestamp())


def warp(vm, iso: str):
    vm.warp(iso)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()


def fixture_json(name: str):
    return json.loads(fixture_text(name))


def modified(name: str, fn) -> str:
    """Load a real fixture, apply fn(data) in memory, return JSON text.
    If fn returns a dict, that dict replaces the data; otherwise data is mutated in place."""
    data = copy.deepcopy(fixture_json(name))
    out = fn(data)
    return json.dumps(out if isinstance(out, dict) else data)


API = r"https://api\.github\.com/repos/"


def _base(owner=OWNER, repo=REPO):
    return API + re.escape(owner) + "/" + re.escape(repo)


def url_release(tag=TAG, owner=OWNER, repo=REPO):
    return _base(owner, repo) + "/releases/tags/" + re.escape(tag) + "$"


def url_commit(tag=TAG, owner=OWNER, repo=REPO):
    return _base(owner, repo) + "/commits/refs/tags/" + re.escape(tag) + "$"


def url_check_runs(sha, owner=OWNER, repo=REPO):
    return _base(owner, repo) + "/commits/" + sha + r"/check-runs\?filter=latest&per_page=100$"


def url_status(sha, owner=OWNER, repo=REPO):
    return _base(owner, repo) + "/commits/" + sha + r"/status\?per_page=100$"


def url_pull(n, owner=OWNER, repo=REPO):
    return _base(owner, repo) + "/pulls/" + str(n) + "$"


def url_commits(author, since_iso, until_iso, owner=OWNER, repo=REPO):
    return (
        _base(owner, repo)
        + r"/commits\?author=" + re.escape(author)
        + "&since=" + re.escape(since_iso)
        + "&until=" + re.escape(until_iso)
        + "&per_page=100$"
    )


def mock(vm, pattern: str, body: str, status: int = 200):
    vm.mock_web(pattern, {"status": status, "body": body})


def contract_module():
    for name, mod in sys.modules.items():
        if name == "_contract_milestone_grant":
            return mod
    raise RuntimeError("contract module not loaded")


def to_hex(addr) -> str:
    if hasattr(addr, "as_hex"):
        return addr.as_hex
    from genlayer.py.types import Address

    return Address(addr).as_hex


def assert_invariants(c, grant_ids=None):
    """Accounting invariants: global and per grant."""
    t = c.get_totals()
    assert t["total_locked"] == t["total_deposited"] - t["total_paid"] - t["total_refunded"]
    assert t["total_locked"] >= 0
    if grant_ids is None:
        grant_ids = [str(i) for i in range(1, t["grant_count"] + 1)]
    locked_sum = 0
    for gid in grant_ids:
        g = c.get_grant(gid)
        assert g["locked"] == g["deposited"] - g["paid"] - g["refunded"]
        assert g["deposited"] == sum(m["amount"] for m in g["milestones"])
        assert g["paid"] == sum(m["paid_amount"] for m in g["milestones"])
        assert g["paid"] == sum(m["amount"] for m in g["milestones"] if m["status"] == "PAID")
        assert g["refunded"] == sum(m["amount"] for m in g["milestones"] if m["status"] in ("RECLAIMED", "CANCELLED"))
        locked_sum += g["locked"]
    if len(grant_ids) == t["grant_count"]:
        assert locked_sum == t["total_locked"]
    return t


@pytest.fixture
def transfers(direct_vm):
    """Capture native GEN transfers (emit_transfer -> EthSend) as (to_hex, value)."""
    sent = []

    def hook(vm, request):
        if isinstance(request, dict) and "EthSend" in request:
            data = request["EthSend"]
            sent.append((to_hex(data.get("address")), int(data.get("value", 0))))
        return None

    direct_vm._gl_call_hook = hook
    return sent


@pytest.fixture
def grant_contract(direct_vm, direct_deploy, direct_owner):
    """Deployed contract, clock at T0."""
    warp(direct_vm, T0)
    direct_vm.sender = direct_owner
    c = direct_deploy(CONTRACT)
    direct_vm.check_pickling = True
    return c


def milestone(amount=GEN, deadline=None, tag=TAG, **kw):
    m = {"amount": amount, "deadline": deadline if deadline is not None else ts("2026-03-01T00:00:00Z"), "tag": tag}
    m.update(kw)
    return m


def create(vm, c, funder, grantee, milestones, value=None, owner=OWNER, repo=REPO, grace_days=7):
    vm.sender = funder
    vm.value = sum(m["amount"] for m in milestones) if value is None else value
    try:
        return c.create_grant(to_hex(grantee), owner, repo, grace_days, milestones)
    finally:
        vm.value = 0


# ---------------------------------------------------------------- real fixture values
SHA = "22d924701a6ae2e4cd01e9a15bbaf3946094af65"  # commit that tag 3.1.3 points to
PUBLISHED_AT = "2026-02-19T05:01:30Z"  # release 3.1.3 published_at
PR = 5924  # "release version 3.1.3", merged into `stable`
PR_MERGED_AT = "2026-02-19T05:01:53Z"
AUTHOR = "davidism"
SINCE = "2026-02-01T00:00:00Z"
UNTIL = "2026-02-20T00:00:00Z"
AUTHOR_COMMITS = 15  # commits by davidism on the default branch in [SINCE, UNTIL]
CHECK_RUN_COUNT = 17  # latest check runs on SHA; 16 success, "Development Versions" failure
FAILING_CHECK = "Development Versions"
REQUIRED_OK = ["build", "typing", "3.12", "3.13", "Windows", "Mac"]


def mock_github(
    vm,
    release=None,
    release_status=200,
    commit=None,
    commit_status=200,
    check_runs=None,
    check_runs_status=200,
    status=None,
    status_status=200,
    pull=None,
    pull_status=200,
    commits=None,
    commits_status=200,
):
    """Register the real pallets/flask fixtures (each body/status can be overridden)."""
    mock(vm, url_release(), release if release is not None else fixture_text("release_flask_3.1.3.json"), release_status)
    mock(vm, url_commit(), commit if commit is not None else fixture_text("commit_flask_3.1.3.json"), commit_status)
    mock(vm, url_check_runs(SHA), check_runs if check_runs is not None else fixture_text("check_runs_flask_3.1.3.json"), check_runs_status)
    mock(vm, url_status(SHA), status if status is not None else fixture_text("status_flask_3.1.3.json"), status_status)
    mock(vm, url_pull(PR), pull if pull is not None else fixture_text("pull_flask_5924.json"), pull_status)
    mock(
        vm,
        url_commits(AUTHOR, SINCE, UNTIL),
        commits if commits is not None else fixture_text("commits_flask_davidism_2026-02.json"),
        commits_status,
    )


def full_milestone(amount=GEN, deadline=None, **kw):
    """Milestone using every criterion type; PASSes on the real fixtures."""
    m = milestone(
        amount=amount,
        deadline=deadline,
        checks="both",
        required_checks=list(REQUIRED_OK),
        pr_number=PR,
        pr_base="stable",
        commit_author=AUTHOR,
        min_commits=10,
        commits_since=ts(SINCE),
        commits_until=ts(UNTIL),
    )
    m.update(kw)
    return m


def claim(vm, c, grantee, gid, idx=0):
    vm.sender = grantee
    c.claim_milestone(gid, idx)


def verify(vm, c, caller, gid, idx=0):
    vm.sender = caller
    return c.verify_milestone(gid, idx)
