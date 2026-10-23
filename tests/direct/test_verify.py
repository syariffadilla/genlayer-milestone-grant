"""verify_milestone(): PASS / FAIL / DEFER on real captured GitHub responses.

Real fixture facts (pallets/flask, tag 3.1.3), see fixtures/README.md:
  release published_at 2026-02-19T05:01:30Z, tag commit 22d92470...,
  17 latest check runs (16 success, "Development Versions" = failure),
  combined status success (1 context), PR #5924 merged into `stable` at
  2026-02-19T05:01:53Z, 15 commits by davidism in [2026-02-01, 2026-02-20).
"""

import json

import pytest

from tests.direct.conftest import (
    AUTHOR_COMMITS,
    CHECK_RUN_COUNT,
    FAILING_CHECK,
    GEN,
    PR_MERGED_AT,
    PUBLISHED_AT,
    SHA,
    assert_invariants,
    claim,
    create,
    fixture_text,
    full_milestone,
    milestone,
    mock_github,
    modified,
    to_hex,
    ts,
    verify,
    warp,
)

AFTER_RELEASE = "2026-02-20T00:00:00Z"


def _setup(vm, c, funder, grantee, ms, claim_idx=0):
    gid = create(vm, c, funder, grantee, ms)
    claim(vm, c, grantee, gid, claim_idx)
    warp(vm, AFTER_RELEASE)
    return gid


# ------------------------------------------------------------------ PASS


def test_pass_release_only_pays_grantee(direct_vm, grant_contract, direct_owner, direct_alice, direct_bob, transfers):
    gid = _setup(direct_vm, grant_contract, direct_owner, direct_alice, [milestone(amount=2 * GEN), milestone(amount=3 * GEN)])
    mock_github(direct_vm)
    ev = verify(direct_vm, grant_contract, direct_bob, gid)  # anyone may verify
    assert ev["verdict"] == "PASS" and ev["reason"] == "" and ev["payout"] == 2 * GEN
    assert (ev["grant_id"], ev["milestone"], ev["owner"], ev["repo"], ev["tag"]) == (gid, 0, "pallets", "flask", "3.1.3")
    assert ev["release_http"] == 200 and ev["release_tag"] == "3.1.3" and ev["release_draft"] is False
    assert ev["release_published_at"] == PUBLISHED_AT and ev["release_published_ts"] == ts(PUBLISHED_AT)
    assert ev["commit_http"] == 200 and ev["commit_sha"] == SHA
    assert ev["check_runs_http"] == 0 and ev["status_http"] == 0 and ev["pr_http"] == 0 and ev["commits_http"] == 0
    assert transfers == [(to_hex(direct_alice), 2 * GEN)]  # to the grantee, not the caller
    m = grant_contract.get_milestone(gid, 0)
    assert m["status"] == "PAID" and m["paid_amount"] == 2 * GEN and m["verify_attempts"] == 1
    assert m["last_verdict"] == "PASS" and json.loads(m["evidence"]) == ev
    assert grant_contract.get_milestone(gid, 1)["status"] == "OPEN"
    t = assert_invariants(grant_contract)
    assert (t["total_paid"], t["total_locked"]) == (2 * GEN, 3 * GEN)


def test_pass_all_criteria(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    gid = _setup(direct_vm, grant_contract, direct_owner, direct_alice, [full_milestone(amount=GEN + 1)])
    mock_github(direct_vm)
    ev = verify(direct_vm, grant_contract, direct_alice, gid)
    assert ev["verdict"] == "PASS", ev["reason"]
    assert ev["check_runs_http"] == 200 and ev["check_runs_total"] == CHECK_RUN_COUNT and len(ev["check_runs"]) == CHECK_RUN_COUNT
    assert ["Development Versions", "completed", "failure"] in ev["check_runs"]  # bound, not required
    assert ["build", "completed", "success"] in ev["check_runs"]
    assert ev["check_runs"] == sorted(ev["check_runs"])
    assert ev["status_http"] == 200 and ev["status_state"] == "success" and ev["status_total"] == 1
    assert ev["statuses"] == [["docs/readthedocs.org:flask", "success"]]
    assert ev["pr_http"] == 200 and ev["pr_number"] == 5924 and ev["pr_merged"] is True
    assert ev["pr_state"] == "closed" and ev["pr_base"] == "stable"
    assert ev["pr_merged_at"] == PR_MERGED_AT and ev["pr_merged_ts"] == ts(PR_MERGED_AT)
    assert ev["commits_http"] == 200 and ev["commits_count"] == AUTHOR_COMMITS
    assert ev["payout"] == GEN + 1
    assert transfers == [(to_hex(direct_alice), GEN + 1)]
    assert_invariants(grant_contract)


def test_pass_exactly_at_deadline(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    gid = create(direct_vm, grant_contract, direct_owner, direct_alice, [milestone(deadline=ts(PUBLISHED_AT))])
    claim(direct_vm, grant_contract, direct_alice, gid)
    warp(direct_vm, AFTER_RELEASE)  # after deadline, inside grace
    mock_github(direct_vm)
    assert verify(direct_vm, grant_contract, direct_alice, gid)["verdict"] == "PASS"
    assert len(transfers) == 1


# ------------------------------------------------------------------ double payment


def test_no_double_payment(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    gid = _setup(direct_vm, grant_contract, direct_owner, direct_alice, [milestone(amount=GEN)])
    mock_github(direct_vm)
    verify(direct_vm, grant_contract, direct_alice, gid)
    with direct_vm.expect_revert("Milestone not claimed (status PAID)"):
        verify(direct_vm, grant_contract, direct_alice, gid)
    with direct_vm.expect_revert("Milestone not open (status PAID)"):
        claim(direct_vm, grant_contract, direct_alice, gid)
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("not reclaimable in status PAID"):
        grant_contract.reclaim_milestone(gid, 0)
    with direct_vm.expect_revert("Only never-claimed open milestones"):
        grant_contract.cancel_milestone(gid, 0)
    assert transfers == [(to_hex(direct_alice), GEN)]
    t = assert_invariants(grant_contract)
    assert t["total_paid"] == GEN and t["total_locked"] == 0


# ------------------------------------------------------------------ FAIL


def _fail_case(vm, c, funder, grantee, m, transfers, reason, **mocks):
    gid = _setup(vm, c, funder, grantee, [m])
    mock_github(vm, **mocks)
    ev = verify(vm, c, grantee, gid)
    assert (ev["verdict"], ev["reason"], ev["payout"]) == ("FAIL", reason, 0)
    st = c.get_milestone(gid, 0)
    assert st["status"] == "OPEN" and st["last_verdict"] == "FAIL" and st["last_reason"] == reason
    assert transfers == []
    t = assert_invariants(c)
    assert t["total_locked"] == m["amount"]
    return gid, ev


def test_fail_any_check_run_failed_real(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    # no required_checks -> every latest check run must pass; the real data has one failure
    _fail_case(direct_vm, grant_contract, direct_owner, direct_alice, milestone(checks="check_runs"), transfers, "CHECK_FAILED:" + FAILING_CHECK)


def test_fail_required_check_failed(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    m = milestone(checks="check_runs", required_checks=["build", FAILING_CHECK])
    _fail_case(direct_vm, grant_contract, direct_owner, direct_alice, m, transfers, "CHECK_FAILED:" + FAILING_CHECK)


def test_fail_required_check_missing(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    m = milestone(checks="check_runs", required_checks=["build", "lint"])
    _fail_case(direct_vm, grant_contract, direct_owner, direct_alice, m, transfers, "REQUIRED_CHECK_MISSING:lint")


def test_fail_no_check_runs(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    body = modified("check_runs_flask_3.1.3.json", lambda d: {"total_count": 0, "check_runs": []})
    _fail_case(direct_vm, grant_contract, direct_owner, direct_alice, milestone(checks="check_runs"), transfers, "CHECKS_NONE", check_runs=body)


def test_fail_release_after_deadline(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    m = milestone(deadline=ts(PUBLISHED_AT) - 1)
    _fail_case(direct_vm, grant_contract, direct_owner, direct_alice, m, transfers, "RELEASE_AFTER_DEADLINE")


def test_fail_release_before_not_before(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    m = milestone(not_before=ts(PUBLISHED_AT) + 1)
    _fail_case(direct_vm, grant_contract, direct_owner, direct_alice, m, transfers, "RELEASE_BEFORE_NOT_BEFORE")


def test_fail_tag_mismatch(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    body = modified("release_flask_3.1.3.json", lambda d: d.update(tag_name="3.1.2"))
    _fail_case(direct_vm, grant_contract, direct_owner, direct_alice, milestone(), transfers, "TAG_MISMATCH", release=body)


def test_fail_release_draft(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    body = modified("release_flask_3.1.3.json", lambda d: d.update(draft=True))
    _fail_case(direct_vm, grant_contract, direct_owner, direct_alice, milestone(), transfers, "RELEASE_IS_DRAFT", release=body)


@pytest.mark.parametrize("state,reason", [("failure", "STATUS_FAILURE"), ("error", "STATUS_ERROR")])
def test_fail_combined_status(direct_vm, grant_contract, direct_owner, direct_alice, transfers, state, reason):
    body = modified("status_flask_3.1.3.json", lambda d: d.update(state=state))
    _fail_case(direct_vm, grant_contract, direct_owner, direct_alice, milestone(checks="status"), transfers, reason, status=body)


def test_fail_no_statuses(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    body = modified("status_flask_3.1.3.json", lambda d: d.update(state="pending", total_count=0, statuses=[]))
    _fail_case(direct_vm, grant_contract, direct_owner, direct_alice, milestone(checks="status"), transfers, "STATUS_NONE", status=body)


def test_fail_pr_wrong_base(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    m = full_milestone(pr_base="main")  # PR #5924 was merged into `stable`
    _fail_case(direct_vm, grant_contract, direct_owner, direct_alice, m, transfers, "PR_WRONG_BASE")


def test_fail_pr_not_merged(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    body = modified("pull_flask_5924.json", lambda d: d.update(merged=False, merged_at=None))
    _fail_case(direct_vm, grant_contract, direct_owner, direct_alice, full_milestone(), transfers, "PR_NOT_MERGED", pull=body)


def test_fail_pr_merged_after_deadline(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    # deadline between release (05:01:30) and merge (05:01:53); commit range must end by deadline
    d = ts(PUBLISHED_AT) + 10
    m = full_milestone(deadline=d, commits_until=d)
    gid = create(direct_vm, grant_contract, direct_owner, direct_alice, [m])
    claim(direct_vm, grant_contract, direct_alice, gid)
    warp(direct_vm, AFTER_RELEASE)
    mock_github(direct_vm)
    from tests.direct.conftest import AUTHOR, SINCE, fixture_text as ft, mock, url_commits

    mock(direct_vm, url_commits(AUTHOR, SINCE, "2026-02-19T05:01:40Z"), ft("commits_flask_davidism_2026-02.json"))
    ev = verify(direct_vm, grant_contract, direct_alice, gid)
    assert (ev["verdict"], ev["reason"]) == ("FAIL", "PR_MERGED_AFTER_DEADLINE")
    assert transfers == []


def test_fail_too_few_commits(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    m = full_milestone(min_commits=AUTHOR_COMMITS + 1)
    _fail_case(direct_vm, grant_contract, direct_owner, direct_alice, m, transfers, "TOO_FEW_COMMITS")


def test_fail_wins_over_defer(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    # checks still running (DEFER) but PR merged into the wrong base (FAIL) -> FAIL
    body = modified("check_runs_flask_3.1.3.json", lambda d: [r.update(status="in_progress", conclusion=None) for r in d["check_runs"]])
    _fail_case(direct_vm, grant_contract, direct_owner, direct_alice, full_milestone(pr_base="main"), transfers, "PR_WRONG_BASE", check_runs=body)


def test_fail_then_reclaim_then_pass(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    gid, _ = _fail_case(
        direct_vm, grant_contract, direct_owner, direct_alice, milestone(checks="check_runs", amount=GEN), transfers, "CHECK_FAILED:" + FAILING_CHECK
    )
    # grantee fixes CI (validator/leader now see the run re-run green) and claims again
    claim(direct_vm, grant_contract, direct_alice, gid)
    direct_vm.clear_mocks()
    green = modified(
        "check_runs_flask_3.1.3.json",
        lambda d: [r.update(conclusion="success") for r in d["check_runs"] if r["name"] == FAILING_CHECK],
    )
    mock_github(direct_vm, check_runs=green)
    ev = verify(direct_vm, grant_contract, direct_alice, gid)
    assert ev["verdict"] == "PASS"
    m = grant_contract.get_milestone(gid, 0)
    assert m["claims"] == 2 and m["verify_attempts"] == 2 and m["status"] == "PAID"
    assert transfers == [(to_hex(direct_alice), GEN)]
    assert_invariants(grant_contract)


# ------------------------------------------------------------------ DEFER (no money moves, retryable)


def _defer_case(vm, c, funder, grantee, m, transfers, reason, **mocks):
    gid = _setup(vm, c, funder, grantee, [m])
    mock_github(vm, **mocks)
    ev = verify(vm, c, grantee, gid)
    assert (ev["verdict"], ev["reason"], ev["payout"]) == ("DEFER", reason, 0)
    st = c.get_milestone(gid, 0)
    assert st["status"] == "CLAIMED" and st["last_verdict"] == "DEFER" and st["last_reason"] == reason
    assert transfers == []
    assert_invariants(c)
    return gid, ev


def test_defer_rate_limit_403_real_body(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    gid, ev = _defer_case(
        direct_vm, grant_contract, direct_owner, direct_alice, milestone(), transfers, "GITHUB_RATE_LIMIT:release",
        release=fixture_text("ratelimit_403.json"), release_status=403,
    )
    assert ev["fetch_error"] == "GITHUB_RATE_LIMIT:release" and ev["release_http"] == 403
    assert ev["commit_http"] == 0 and ev["commit_sha"] == ""  # stops at the first failed request


def test_defer_rate_limit_429(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    _defer_case(
        direct_vm, grant_contract, direct_owner, direct_alice, full_milestone(), transfers, "GITHUB_RATE_LIMIT:check_runs",
        check_runs="", check_runs_status=429,
    )


def test_defer_rate_limit_on_last_request(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    _, ev = _defer_case(
        direct_vm, grant_contract, direct_owner, direct_alice, full_milestone(), transfers, "GITHUB_RATE_LIMIT:commits",
        commits=fixture_text("ratelimit_403.json"), commits_status=403,
    )
    assert ev["pr_http"] == 200 and ev["commits_http"] == 403


def test_defer_forbidden_not_rate_limit(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    _defer_case(
        direct_vm, grant_contract, direct_owner, direct_alice, milestone(), transfers, "GITHUB_HTTP_403:release",
        release='{"message":"Forbidden"}', release_status=403,
    )


def test_defer_release_404_real_body(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    _defer_case(
        direct_vm, grant_contract, direct_owner, direct_alice, milestone(), transfers, "GITHUB_NOT_FOUND:release",
        release=fixture_text("release_flask_404.json"), release_status=404,
    )


def test_defer_pull_404(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    _defer_case(
        direct_vm, grant_contract, direct_owner, direct_alice, full_milestone(), transfers, "GITHUB_NOT_FOUND:pull",
        pull=fixture_text("release_flask_404.json"), pull_status=404,
    )


def test_defer_server_error(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    _defer_case(
        direct_vm, grant_contract, direct_owner, direct_alice, milestone(checks="status"), transfers, "GITHUB_HTTP_502:status",
        status="Bad Gateway", status_status=502,
    )


def test_defer_bad_json(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    _defer_case(direct_vm, grant_contract, direct_owner, direct_alice, milestone(), transfers, "GITHUB_BAD_RESPONSE:commit", commit="<html>")


def test_defer_bad_shape(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    body = modified("commit_flask_3.1.3.json", lambda d: d.update(sha="xyz"))
    _defer_case(direct_vm, grant_contract, direct_owner, direct_alice, milestone(), transfers, "GITHUB_BAD_RESPONSE:commit", commit=body)


def test_defer_unreachable(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    gid = _setup(direct_vm, grant_contract, direct_owner, direct_alice, [milestone()])
    # no mocks registered: the web request raises -> GITHUB_UNREACHABLE
    ev = verify(direct_vm, grant_contract, direct_alice, gid)
    assert (ev["verdict"], ev["reason"]) == ("DEFER", "GITHUB_UNREACHABLE:release")
    assert transfers == []


def test_defer_check_pending(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    body = modified(
        "check_runs_flask_3.1.3.json",
        lambda d: [r.update(status="in_progress", conclusion=None) for r in d["check_runs"] if r["name"] == "build"],
    )
    m = milestone(checks="check_runs", required_checks=["build", "typing"])
    _, ev = _defer_case(direct_vm, grant_contract, direct_owner, direct_alice, m, transfers, "CHECK_PENDING:build", check_runs=body)
    assert ["build", "in_progress", ""] in ev["check_runs"]


def test_defer_checks_truncated(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    body = modified("check_runs_flask_3.1.3.json", lambda d: d.update(total_count=150))
    _defer_case(direct_vm, grant_contract, direct_owner, direct_alice, milestone(checks="both"), transfers, "CHECKS_TRUNCATED", check_runs=body)


def test_defer_status_pending(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    body = modified("status_flask_3.1.3.json", lambda d: d.update(state="pending"))
    _defer_case(direct_vm, grant_contract, direct_owner, direct_alice, milestone(checks="status"), transfers, "STATUS_PENDING", status=body)


def test_defer_then_retry_pass(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    gid, _ = _defer_case(
        direct_vm, grant_contract, direct_owner, direct_alice, milestone(amount=GEN), transfers, "GITHUB_RATE_LIMIT:release",
        release=fixture_text("ratelimit_403.json"), release_status=403,
    )
    direct_vm.clear_mocks()
    mock_github(direct_vm)
    ev = verify(direct_vm, grant_contract, direct_owner, gid)  # retry, still CLAIMED, no re-claim needed
    assert ev["verdict"] == "PASS"
    m = grant_contract.get_milestone(gid, 0)
    assert m["verify_attempts"] == 2 and m["claims"] == 1 and m["status"] == "PAID"
    assert transfers == [(to_hex(direct_alice), GEN)]
    assert_invariants(grant_contract)


# ------------------------------------------------------------------ preconditions


def test_verify_requires_claim(direct_vm, grant_contract, direct_owner, direct_alice):
    gid = create(direct_vm, grant_contract, direct_owner, direct_alice, [milestone()])
    mock_github(direct_vm)
    with direct_vm.expect_revert("Milestone not claimed (status OPEN)"):
        verify(direct_vm, grant_contract, direct_alice, gid)


def test_verify_window_closed(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    gid = create(direct_vm, grant_contract, direct_owner, direct_alice, [milestone()])
    claim(direct_vm, grant_contract, direct_alice, gid)
    warp(direct_vm, "2026-03-08T00:00:01Z")  # deadline 03-01 + 7 days grace, +1 s
    mock_github(direct_vm)
    with direct_vm.expect_revert("Verification window closed"):
        verify(direct_vm, grant_contract, direct_alice, gid)
    assert transfers == []


def test_verify_last_second_of_grace(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    gid = create(direct_vm, grant_contract, direct_owner, direct_alice, [milestone()])
    claim(direct_vm, grant_contract, direct_alice, gid)
    warp(direct_vm, "2026-03-08T00:00:00Z")
    mock_github(direct_vm)
    assert verify(direct_vm, grant_contract, direct_alice, gid)["verdict"] == "PASS"
    assert len(transfers) == 1


def test_verify_unknown(direct_vm, grant_contract, direct_owner, direct_alice):
    with direct_vm.expect_revert("Unknown grant"):
        verify(direct_vm, grant_contract, direct_alice, "7")
    gid = create(direct_vm, grant_contract, direct_owner, direct_alice, [milestone()])
    with direct_vm.expect_revert("Unknown milestone"):
        verify(direct_vm, grant_contract, direct_alice, gid, 3)
