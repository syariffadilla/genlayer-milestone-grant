"""claim_milestone(), cancel_milestone(), reclaim_milestone(): access control, timing, transfers, accounting."""

from tests.direct.conftest import (
    GEN,
    FAILING_CHECK,
    assert_invariants,
    claim,
    create,
    milestone,
    mock_github,
    to_hex,
    ts,
    verify,
    warp,
)

DEADLINE = "2026-03-01T00:00:00Z"
GRACE_END = "2026-03-08T00:00:00Z"  # deadline + 7 days


def _grant(vm, c, funder, grantee, amounts=(GEN, 2 * GEN)):
    return create(vm, c, funder, grantee, [milestone(amount=a) for a in amounts])


# ------------------------------------------------------------------ claim


def test_claim_only_grantee(direct_vm, grant_contract, direct_owner, direct_alice, direct_bob):
    gid = _grant(direct_vm, grant_contract, direct_owner, direct_alice)
    for who in (direct_owner, direct_bob):
        with direct_vm.expect_revert("Only grantee"):
            claim(direct_vm, grant_contract, who, gid)
    claim(direct_vm, grant_contract, direct_alice, gid)
    m = grant_contract.get_milestone(gid, 0)
    assert m["status"] == "CLAIMED" and m["claims"] == 1


def test_claim_twice_rejected(direct_vm, grant_contract, direct_owner, direct_alice):
    gid = _grant(direct_vm, grant_contract, direct_owner, direct_alice)
    claim(direct_vm, grant_contract, direct_alice, gid)
    with direct_vm.expect_revert("Milestone not open (status CLAIMED)"):
        claim(direct_vm, grant_contract, direct_alice, gid)


def test_claim_after_deadline_rejected(direct_vm, grant_contract, direct_owner, direct_alice):
    gid = _grant(direct_vm, grant_contract, direct_owner, direct_alice)
    warp(direct_vm, DEADLINE)
    claim(direct_vm, grant_contract, direct_alice, gid, 1)  # exactly at the deadline is allowed
    warp(direct_vm, "2026-03-01T00:00:01Z")
    with direct_vm.expect_revert("Deadline passed"):
        claim(direct_vm, grant_contract, direct_alice, gid, 0)


# ------------------------------------------------------------------ cancel


def test_cancel_unclaimed_future_milestone(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    gid = _grant(direct_vm, grant_contract, direct_owner, direct_alice)
    direct_vm.sender = direct_owner
    assert grant_contract.cancel_milestone(gid, 1) == 2 * GEN
    assert transfers == [(to_hex(direct_owner), 2 * GEN)]
    assert grant_contract.get_milestone(gid, 1)["status"] == "CANCELLED"
    g = grant_contract.get_grant(gid)
    assert (g["refunded"], g["locked"]) == (2 * GEN, GEN)
    with direct_vm.expect_revert("Only never-claimed open milestones"):
        grant_contract.cancel_milestone(gid, 1)  # no double refund
    with direct_vm.expect_revert("Milestone not open (status CANCELLED)"):
        claim(direct_vm, grant_contract, direct_alice, gid, 1)
    assert len(transfers) == 1
    t = assert_invariants(grant_contract)
    assert t["total_refunded"] == 2 * GEN and t["total_locked"] == GEN


def test_cancel_only_funder(direct_vm, grant_contract, direct_owner, direct_alice, direct_bob, transfers):
    gid = _grant(direct_vm, grant_contract, direct_owner, direct_alice)
    for who in (direct_alice, direct_bob):
        direct_vm.sender = who
        with direct_vm.expect_revert("Only funder"):
            grant_contract.cancel_milestone(gid, 0)
    assert transfers == []


def test_cancel_claimed_rejected(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    gid = _grant(direct_vm, grant_contract, direct_owner, direct_alice)
    claim(direct_vm, grant_contract, direct_alice, gid)
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("Only never-claimed open milestones"):
        grant_contract.cancel_milestone(gid, 0)
    assert transfers == []


def test_cancel_after_failed_claim_rejected(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    # a milestone that was claimed once (then FAILed back to OPEN) can no longer be cancelled
    gid = create(direct_vm, grant_contract, direct_owner, direct_alice, [milestone(checks="check_runs")])
    claim(direct_vm, grant_contract, direct_alice, gid)
    warp(direct_vm, "2026-02-20T00:00:00Z")
    mock_github(direct_vm)
    ev = verify(direct_vm, grant_contract, direct_alice, gid)
    assert (ev["verdict"], ev["reason"]) == ("FAIL", "CHECK_FAILED:" + FAILING_CHECK)
    assert grant_contract.get_milestone(gid, 0)["status"] == "OPEN"
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("Only never-claimed open milestones"):
        grant_contract.cancel_milestone(gid, 0)
    assert transfers == []


def test_cancel_at_or_after_deadline_rejected(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    gid = _grant(direct_vm, grant_contract, direct_owner, direct_alice)
    warp(direct_vm, DEADLINE)
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("Deadline passed; use reclaim_milestone"):
        grant_contract.cancel_milestone(gid, 0)
    assert transfers == []


# ------------------------------------------------------------------ reclaim


def test_reclaim_before_grace_end_rejected(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    gid = _grant(direct_vm, grant_contract, direct_owner, direct_alice)
    direct_vm.sender = direct_owner
    for t in ("2026-02-01T00:00:00Z", DEADLINE, GRACE_END):  # grace end itself is still inside
        warp(direct_vm, t)
        with direct_vm.expect_revert("Deadline + grace not passed yet"):
            grant_contract.reclaim_milestone(gid, 0)
    assert transfers == []


def test_reclaim_open_and_claimed_after_grace(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    gid = _grant(direct_vm, grant_contract, direct_owner, direct_alice)
    claim(direct_vm, grant_contract, direct_alice, gid, 1)  # claimed but never verified (or DEFER forever)
    warp(direct_vm, "2026-03-08T00:00:01Z")
    direct_vm.sender = direct_owner
    assert grant_contract.reclaim_milestone(gid, 0) == GEN
    assert grant_contract.reclaim_milestone(gid, 1) == 2 * GEN
    assert transfers == [(to_hex(direct_owner), GEN), (to_hex(direct_owner), 2 * GEN)]
    g = grant_contract.get_grant(gid)
    assert [m["status"] for m in g["milestones"]] == ["RECLAIMED", "RECLAIMED"]
    assert (g["refunded"], g["locked"], g["paid"]) == (3 * GEN, 0, 0)
    with direct_vm.expect_revert("not reclaimable in status RECLAIMED"):
        grant_contract.reclaim_milestone(gid, 0)  # no double refund
    with direct_vm.expect_revert("Milestone not claimed (status RECLAIMED)"):
        verify(direct_vm, grant_contract, direct_alice, gid, 1)
    assert len(transfers) == 2
    t = assert_invariants(grant_contract)
    assert t["total_locked"] == 0


def test_reclaim_only_funder(direct_vm, grant_contract, direct_owner, direct_alice, direct_bob, transfers):
    gid = _grant(direct_vm, grant_contract, direct_owner, direct_alice)
    warp(direct_vm, "2026-04-01T00:00:00Z")
    for who in (direct_alice, direct_bob):
        direct_vm.sender = who
        with direct_vm.expect_revert("Only funder"):
            grant_contract.reclaim_milestone(gid, 0)
    assert transfers == []


def test_reclaim_respects_grace_days(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    gid = create(direct_vm, grant_contract, direct_owner, direct_alice, [milestone(amount=GEN)], grace_days=30)
    direct_vm.sender = direct_owner
    warp(direct_vm, "2026-03-31T00:00:00Z")  # deadline + 30 days exactly
    with direct_vm.expect_revert("Deadline + grace not passed yet"):
        grant_contract.reclaim_milestone(gid, 0)
    warp(direct_vm, "2026-03-31T00:00:01Z")
    assert grant_contract.reclaim_milestone(gid, 0) == GEN
    assert transfers == [(to_hex(direct_owner), GEN)]


def test_mixed_lifecycle_accounting(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    """One grant: milestone 0 paid, 1 cancelled, 2 reclaimed. Every wei accounted for."""
    ms = [milestone(amount=GEN), milestone(amount=2 * GEN), milestone(amount=4 * GEN, tag="9.9.9")]
    gid = create(direct_vm, grant_contract, direct_owner, direct_alice, ms)
    direct_vm.sender = direct_owner
    grant_contract.cancel_milestone(gid, 1)
    claim(direct_vm, grant_contract, direct_alice, gid, 0)
    claim(direct_vm, grant_contract, direct_alice, gid, 2)
    warp(direct_vm, "2026-02-20T00:00:00Z")
    mock_github(direct_vm)
    assert verify(direct_vm, grant_contract, direct_alice, gid, 0)["verdict"] == "PASS"
    # tag 9.9.9 has no mock -> request raises -> DEFER, funds stay locked
    assert verify(direct_vm, grant_contract, direct_alice, gid, 2)["verdict"] == "DEFER"
    assert_invariants(grant_contract)
    warp(direct_vm, "2026-03-09T00:00:00Z")
    direct_vm.sender = direct_owner
    grant_contract.reclaim_milestone(gid, 2)
    assert transfers == [
        (to_hex(direct_owner), 2 * GEN),
        (to_hex(direct_alice), GEN),
        (to_hex(direct_owner), 4 * GEN),
    ]
    g = grant_contract.get_grant(gid)
    assert (g["deposited"], g["paid"], g["refunded"], g["locked"]) == (7 * GEN, GEN, 6 * GEN, 0)
    assert sum(v for _, v in transfers) == g["deposited"]
    t = assert_invariants(grant_contract)
    assert t == {"grant_count": 1, "total_deposited": 7 * GEN, "total_paid": GEN, "total_refunded": 6 * GEN, "total_locked": 0}
    assert ts(DEADLINE) == g["milestones"][0]["deadline"]
