"""Validator side of verify_milestone()'s gl.vm.run_nondet block.

direct_vm.run_validator() runs the captured validator_fn the way a GenLayer
validator would: it re-fetches GitHub itself (through the CURRENT mocks) and
compares its own evidence with the leader's. Swapping mocks between the
contract call and run_validator() simulates a validator that sees different
GitHub data; passing leader_result simulates a leader that reports
something else.
"""

import copy

import pytest

from tests.direct.conftest import (
    GEN,
    claim,
    create,
    contract_module,
    full_milestone,
    milestone,
    mock_github,
    modified,
    fixture_text,
    verify,
    warp,
)


@pytest.fixture
def passed(direct_vm, grant_contract, direct_owner, direct_alice):
    """A milestone using every criterion, verified PASS on the real fixtures."""
    gid = create(direct_vm, grant_contract, direct_owner, direct_alice, [full_milestone(amount=GEN)])
    claim(direct_vm, grant_contract, direct_alice, gid)
    warp(direct_vm, "2026-02-20T00:00:00Z")
    mock_github(direct_vm)
    ev = verify(direct_vm, grant_contract, direct_alice, gid)
    assert ev["verdict"] == "PASS"
    return ev


def _swap(vm, **mocks):
    vm.clear_mocks()
    mock_github(vm, **mocks)


# ------------------------------------------------------------------ agreement


def test_validator_agrees_on_identical_data(direct_vm, passed):
    assert direct_vm.run_validator() is True


def test_validator_ignores_unbound_volatile_fields(direct_vm, passed):
    # fields that do not drive settlement (timestamps of runs, download counts, reactions) may differ
    rel = modified("release_flask_3.1.3.json", lambda d: [a.update(download_count=a["download_count"] + 50) for a in d["assets"]])
    runs = modified("check_runs_flask_3.1.3.json", lambda d: [r.update(started_at="2027-01-01T00:00:00Z") for r in d["check_runs"]])
    runs2 = modified("check_runs_flask_3.1.3.json", lambda d: d["check_runs"].reverse())  # order differs
    pr = modified("pull_flask_5924.json", lambda d: d.update(comments=99, updated_at="2027-01-01T00:00:00Z"))
    _swap(direct_vm, release=rel, check_runs=runs, pull=pr)
    assert direct_vm.run_validator() is True
    _swap(direct_vm, check_runs=runs2)
    assert direct_vm.run_validator() is True


def test_validator_agrees_on_same_defer(direct_vm, grant_contract, direct_owner, direct_alice):
    gid = create(direct_vm, grant_contract, direct_owner, direct_alice, [milestone()])
    claim(direct_vm, grant_contract, direct_alice, gid)
    warp(direct_vm, "2026-02-20T00:00:00Z")
    mock_github(direct_vm, release=fixture_text("ratelimit_403.json"), release_status=403)
    assert verify(direct_vm, grant_contract, direct_alice, gid)["verdict"] == "DEFER"
    assert direct_vm.run_validator() is True


# ------------------------------------------------------------------ validator sees different GitHub data


def _commits_minus_one():
    import json

    data = json.loads(fixture_text("commits_flask_davidism_2026-02.json"))
    return json.dumps(data[:-1])



DIFFERENT_DATA = [
    ("release published later", dict(release=modified("release_flask_3.1.3.json", lambda d: d.update(published_at="2026-02-19T05:01:31Z")))),
    ("release tag", dict(release=modified("release_flask_3.1.3.json", lambda d: d.update(tag_name="3.1.3-rc")))),
    ("release draft", dict(release=modified("release_flask_3.1.3.json", lambda d: d.update(draft=True)))),
    ("tag moved to other commit", dict(commit=modified("commit_flask_3.1.3.json", lambda d: d.update(sha="f" * 40)))),
    ("non-required check conclusion", dict(check_runs=modified("check_runs_flask_3.1.3.json", lambda d: [r.update(conclusion="success") for r in d["check_runs"] if r["name"] == "Development Versions"]))),
    ("check run renamed", dict(check_runs=modified("check_runs_flask_3.1.3.json", lambda d: d["check_runs"][0].update(name="renamed")))),
    ("extra check run", dict(check_runs=modified("check_runs_flask_3.1.3.json", lambda d: (d["check_runs"].append(copy.deepcopy(d["check_runs"][0])), d.update(total_count=18))))),
    ("check run count", dict(check_runs=modified("check_runs_flask_3.1.3.json", lambda d: d.update(total_count=18)))),
    ("check run status", dict(check_runs=modified("check_runs_flask_3.1.3.json", lambda d: d["check_runs"][3].update(status="queued")))),
    ("combined state", dict(status=modified("status_flask_3.1.3.json", lambda d: d.update(state="pending")))),
    ("status count", dict(status=modified("status_flask_3.1.3.json", lambda d: d.update(total_count=2)))),
    ("status context", dict(status=modified("status_flask_3.1.3.json", lambda d: d["statuses"][0].update(context="ci/other")))),
    ("pr merged_at", dict(pull=modified("pull_flask_5924.json", lambda d: d.update(merged_at="2026-02-19T05:01:54Z")))),
    ("pr base", dict(pull=modified("pull_flask_5924.json", lambda d: d["base"].update(ref="main")))),
    ("pr state", dict(pull=modified("pull_flask_5924.json", lambda d: d.update(state="open")))),
    ("pr merged flag", dict(pull=modified("pull_flask_5924.json", lambda d: d.update(merged=False)))),
    ("commit count", dict(commits=_commits_minus_one())),
    ("validator rate limited", dict(release=fixture_text("ratelimit_403.json"), release_status=403)),
    ("validator gets 404 on pull", dict(pull=fixture_text("release_flask_404.json"), pull_status=404)),
    ("validator gets 500 on commits", dict(commits="oops", commits_status=500)),
]


@pytest.mark.parametrize("label,mocks", DIFFERENT_DATA, ids=[x[0] for x in DIFFERENT_DATA])
def test_validator_rejects_different_github_data(direct_vm, passed, label, mocks):
    _swap(direct_vm, **mocks)
    assert direct_vm.run_validator() is False


def test_validator_rejects_leader_error(direct_vm, passed):
    assert direct_vm.run_validator(leader_error=Exception("boom")) is False


def test_validator_rejects_leader_defer_when_data_available(direct_vm, grant_contract, direct_owner, direct_alice):
    gid = create(direct_vm, grant_contract, direct_owner, direct_alice, [milestone()])
    claim(direct_vm, grant_contract, direct_alice, gid)
    warp(direct_vm, "2026-02-20T00:00:00Z")
    mock_github(direct_vm, release=fixture_text("ratelimit_403.json"), release_status=403)
    assert verify(direct_vm, grant_contract, direct_alice, gid)["verdict"] == "DEFER"
    _swap(direct_vm)  # the validator itself is not rate limited
    assert direct_vm.run_validator() is False


# ------------------------------------------------------------------ every bound field, tampered by the leader


def _alt(value):
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, str):
        return value + "x"
    if isinstance(value, list):
        return value[:-1] if value else [["x", "completed", "success"]]
    raise AssertionError(type(value))


def test_every_evidence_key_is_bound(direct_vm, passed):
    keys = contract_module().EVIDENCE_KEYS
    assert sorted(passed.keys()) == sorted(keys)
    for k in keys:
        t = copy.deepcopy(passed)
        t[k] = _alt(t[k])
        assert direct_vm.run_validator(leader_result=t) is False, k


@pytest.mark.parametrize(
    "path,value",
    [
        (("check_runs", 0, 2), "failure"),  # one conclusion
        (("check_runs", 5, 0), "3.12 "),  # one name
        (("check_runs", 1, 1), "in_progress"),  # one status
        (("statuses", 0, 1), "failure"),
    ],
)
def test_nested_fields_bound(direct_vm, passed, path, value):
    t = copy.deepcopy(passed)
    obj = t
    for p in path[:-1]:
        obj = obj[p]
    obj[path[-1]] = value
    assert direct_vm.run_validator(leader_result=t) is False


def test_types_are_strict(direct_vm, passed):
    t = copy.deepcopy(passed)
    t["milestone"] = "0"  # string instead of int
    assert direct_vm.run_validator(leader_result=t) is False
    t = copy.deepcopy(passed)
    t["release_draft"] = 0  # int instead of bool
    assert direct_vm.run_validator(leader_result=t) is False


def test_extra_or_missing_key_rejected(direct_vm, passed):
    t = copy.deepcopy(passed)
    t["note"] = "trust me"
    assert direct_vm.run_validator(leader_result=t) is False
    t = copy.deepcopy(passed)
    del t["commit_sha"]
    assert direct_vm.run_validator(leader_result=t) is False


def test_non_dict_rejected(direct_vm, passed):
    assert direct_vm.run_validator(leader_result=["PASS"]) is False
    assert direct_vm.run_validator(leader_result=None) is False


def test_fake_pass_on_failing_data_rejected(direct_vm, grant_contract, direct_owner, direct_alice):
    # real data FAILs (Development Versions failed); a leader claiming PASS with payout is rejected
    gid = create(direct_vm, grant_contract, direct_owner, direct_alice, [milestone(checks="check_runs", amount=GEN)])
    claim(direct_vm, grant_contract, direct_alice, gid)
    warp(direct_vm, "2026-02-20T00:00:00Z")
    mock_github(direct_vm)
    ev = verify(direct_vm, grant_contract, direct_alice, gid)
    assert ev["verdict"] == "FAIL"
    assert direct_vm.run_validator() is True
    fake = dict(ev, verdict="PASS", reason="", payout=GEN)
    assert direct_vm.run_validator(leader_result=fake) is False
    fake2 = copy.deepcopy(fake)
    fake2["check_runs"] = [[n, s, "success"] for n, s, _ in fake2["check_runs"]]
    assert direct_vm.run_validator(leader_result=fake2) is False


# ------------------------------------------------------------------ pure evaluation helpers


def test_evaluate_matches_contract_and_is_pure(direct_vm, passed):
    mod = contract_module()
    crit = {
        "owner": "pallets", "repo": "flask", "tag": "3.1.3", "deadline": passed["deadline"], "not_before": 0,
        "checks": "both", "required_checks": ["build", "typing", "3.12", "3.13", "Windows", "Mac"],
        "pr_number": 5924, "pr_base": "stable", "commit_author": "davidism", "min_commits": 10,
        "commits_since": 0, "commits_until": 0, "amount": GEN,
    }
    assert mod._evaluate(passed, crit) == ["PASS", "", GEN]
    assert mod._evaluate(passed, dict(crit, amount=5)) == ["PASS", "", 5]
    assert mod._evaluate(passed, dict(crit, required_checks=[])) == ["FAIL", "CHECK_FAILED:Development Versions", 0]
    assert mod._evaluate(dict(passed, fetch_error="GITHUB_RATE_LIMIT:pull"), crit) == ["DEFER", "GITHUB_RATE_LIMIT:pull", 0]


def test_iso_parser_strict(direct_vm, passed):
    mod = contract_module()
    assert mod._iso_to_ts("2026-02-19T05:01:30Z") == 1771477290
    for bad in ["2026-02-19 05:01:30Z", "2026-02-19T05:01:30+00:00", "2026-02-30T00:00:00Z", None, 5, "", "2026-2-19T05:01:30Z"]:
        assert mod._iso_to_ts(bad) == -1
    assert mod._ts_to_iso(1771477290) == "2026-02-19T05:01:30Z"
