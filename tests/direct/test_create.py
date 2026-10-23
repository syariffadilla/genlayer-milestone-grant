"""create_grant(): deposits, stored criteria, input validation, insufficient / excess deposit."""

import pytest

from tests.direct.conftest import (
    AUTHOR,
    GEN,
    PR,
    SINCE,
    UNTIL,
    assert_invariants,
    create,
    full_milestone,
    milestone,
    to_hex,
    ts,
)


def test_create_single_milestone(direct_vm, grant_contract, direct_owner, direct_alice, transfers):
    gid = create(direct_vm, grant_contract, direct_owner, direct_alice, [milestone(amount=3 * GEN)])
    assert gid == "1"
    g = grant_contract.get_grant(gid)
    assert g["funder"] == to_hex(direct_owner)
    assert g["grantee"] == to_hex(direct_alice)
    assert (g["owner"], g["repo"]) == ("pallets", "flask")
    assert g["grace_seconds"] == 7 * 86400
    assert g["created_at"] == ts("2026-01-01T00:00:00Z")
    assert (g["deposited"], g["paid"], g["refunded"], g["locked"]) == (3 * GEN, 0, 0, 3 * GEN)
    m = g["milestones"][0]
    assert m["status"] == "OPEN" and m["amount"] == 3 * GEN and m["tag"] == "3.1.3"
    assert m["checks"] == "none" and m["required_checks"] == [] and m["pr_number"] == 0
    assert m["reclaimable_after"] == m["deadline"] + 7 * 86400
    assert transfers == []  # deposit stays in the contract
    t = assert_invariants(grant_contract)
    assert t == {"grant_count": 1, "total_deposited": 3 * GEN, "total_paid": 0, "total_refunded": 0, "total_locked": 3 * GEN}


def test_create_multi_milestone_full_criteria(direct_vm, grant_contract, direct_owner, direct_alice):
    ms = [
        full_milestone(amount=GEN),
        milestone(amount=2 * GEN, tag="3.2.0", deadline=ts("2026-06-01T00:00:00Z"), checks="check_runs"),
        milestone(amount=5 * GEN + 7, tag="4.0.0", deadline=ts("2026-12-01T00:00:00Z"), not_before=ts("2026-06-01T00:00:00Z")),
    ]
    gid = create(direct_vm, grant_contract, direct_owner, direct_alice, ms, grace_days=30)
    g = grant_contract.get_grant(gid)
    assert g["deposited"] == 8 * GEN + 7
    assert [m["amount"] for m in g["milestones"]] == [GEN, 2 * GEN, 5 * GEN + 7]
    m0 = g["milestones"][0]
    assert m0["checks"] == "both" and m0["pr_number"] == PR and m0["pr_base"] == "stable"
    assert m0["commit_author"] == AUTHOR and m0["min_commits"] == 10
    assert (m0["commits_since"], m0["commits_until"]) == (ts(SINCE), ts(UNTIL))
    assert g["milestones"][2]["not_before"] == ts("2026-06-01T00:00:00Z")
    urls = grant_contract.get_static_urls(gid, 0)
    assert urls == [
        "https://api.github.com/repos/pallets/flask/releases/tags/3.1.3",
        "https://api.github.com/repos/pallets/flask/commits/refs/tags/3.1.3",
        "https://api.github.com/repos/pallets/flask/pulls/5924",
        "https://api.github.com/repos/pallets/flask/commits?author=davidism&since=2026-02-01T00:00:00Z&until=2026-02-20T00:00:00Z&per_page=100",
    ]
    assert_invariants(grant_contract)


def test_two_grants_accounting(direct_vm, grant_contract, direct_owner, direct_alice, direct_bob):
    create(direct_vm, grant_contract, direct_owner, direct_alice, [milestone(amount=GEN)])
    gid2 = create(direct_vm, grant_contract, direct_bob, direct_alice, [milestone(amount=2 * GEN), milestone(amount=GEN)])
    assert gid2 == "2"
    assert grant_contract.get_grant("2")["funder"] == to_hex(direct_bob)
    t = assert_invariants(grant_contract)
    assert t["total_locked"] == 4 * GEN


def test_insufficient_deposit(direct_vm, grant_contract, direct_owner, direct_alice):
    with direct_vm.expect_revert("Insufficient deposit"):
        create(direct_vm, grant_contract, direct_owner, direct_alice, [milestone(amount=GEN), milestone(amount=GEN)], value=2 * GEN - 1)
    assert grant_contract.get_totals()["grant_count"] == 0


def test_zero_deposit(direct_vm, grant_contract, direct_owner, direct_alice):
    with direct_vm.expect_revert("Insufficient deposit"):
        create(direct_vm, grant_contract, direct_owner, direct_alice, [milestone(amount=GEN)], value=0)


def test_excess_deposit_rejected(direct_vm, grant_contract, direct_owner, direct_alice):
    with direct_vm.expect_revert("Deposit exceeds milestone total"):
        create(direct_vm, grant_contract, direct_owner, direct_alice, [milestone(amount=GEN)], value=GEN + 1)
    assert grant_contract.get_totals()["total_deposited"] == 0


def test_grantee_cannot_be_funder(direct_vm, grant_contract, direct_owner):
    with direct_vm.expect_revert("Grantee must differ from funder"):
        create(direct_vm, grant_contract, direct_owner, direct_owner, [milestone()])


def test_invalid_grantee_address(direct_vm, grant_contract, direct_owner):
    direct_vm.sender = direct_owner
    direct_vm.value = GEN
    with direct_vm.expect_revert("Invalid grantee address"):
        grant_contract.create_grant("not-an-address", "pallets", "flask", 7, [milestone()])
    with direct_vm.expect_revert("Invalid grantee address"):
        grant_contract.create_grant("0x" + "00" * 20, "pallets", "flask", 7, [milestone()])
    direct_vm.value = 0


@pytest.mark.parametrize(
    "owner,repo,msg",
    [
        ("", "flask", "Invalid owner"),
        ("pal lets", "flask", "Invalid owner"),
        ("pallets/x", "flask", "Invalid owner"),
        ("pallets", "..", "Invalid repo"),
        ("pallets", "fl?ask", "Invalid repo"),
        ("pallets", "a" * 101, "Invalid repo"),
    ],
)
def test_invalid_repo(direct_vm, grant_contract, direct_owner, direct_alice, owner, repo, msg):
    with direct_vm.expect_revert(msg):
        create(direct_vm, grant_contract, direct_owner, direct_alice, [milestone()], owner=owner, repo=repo)


@pytest.mark.parametrize("grace", [0, 91, -1])
def test_invalid_grace(direct_vm, grant_contract, direct_owner, direct_alice, grace):
    with direct_vm.expect_revert("Invalid grace_days"):
        create(direct_vm, grant_contract, direct_owner, direct_alice, [milestone()], grace_days=grace)


def test_milestone_count_bounds(direct_vm, grant_contract, direct_owner, direct_alice):
    with direct_vm.expect_revert("Need 1-20 milestones"):
        create(direct_vm, grant_contract, direct_owner, direct_alice, [], value=0)
    with direct_vm.expect_revert("Need 1-20 milestones"):
        create(direct_vm, grant_contract, direct_owner, direct_alice, [milestone() for _ in range(21)])
    gid = create(direct_vm, grant_contract, direct_owner, direct_alice, [milestone() for _ in range(20)])
    assert len(grant_contract.get_grant(gid)["milestones"]) == 20


D = ts("2026-03-01T00:00:00Z")

BAD_SPECS = [
    ({"amount": 0}, "Invalid amount"),
    ({"amount": -5}, "Invalid amount"),
    ({"amount": True}, "Invalid amount"),
    ({"deadline": ts("2025-12-31T23:59:59Z")}, "Deadline must be in the future"),
    ({"deadline": ts("2026-01-01T00:00:00Z")}, "Deadline must be in the future"),
    ({"deadline": ts("2030-01-01T00:00:00Z")}, "Deadline too far ahead"),
    ({"tag": ""}, "Invalid tag"),
    ({"tag": "v1.0 beta"}, "Invalid tag"),
    ({"tag": "../x"}, "Invalid tag"),
    ({"checks": "all"}, "Invalid checks mode"),
    ({"required_checks": ["build"]}, "required_checks need checks mode"),
    ({"checks": "status", "required_checks": ["build"]}, "required_checks need checks mode"),
    ({"checks": "check_runs", "required_checks": ["build", "build"]}, "Duplicate required check"),
    ({"checks": "check_runs", "required_checks": [""]}, "Invalid required_checks"),
    ({"checks": "check_runs", "required_checks": "build"}, "Invalid required_checks"),
    ({"not_before": D + 1}, "Invalid not_before"),
    ({"pr_number": -1}, "Invalid pr_number"),
    ({"pr_number": 5}, "Invalid pr_base"),
    ({"pr_base": "main"}, "pr_base given without pr_number"),
    ({"commit_author": "bad login!", "min_commits": 1, "commits_since": 1, "commits_until": 2}, "Invalid commit_author"),
    ({"commit_author": AUTHOR, "min_commits": 0, "commits_since": 1, "commits_until": 2}, "Invalid min_commits"),
    ({"commit_author": AUTHOR, "min_commits": 101, "commits_since": 1, "commits_until": 2}, "Invalid min_commits"),
    ({"commit_author": AUTHOR, "min_commits": 1, "commits_since": 5, "commits_until": 5}, "Invalid commit range"),
    ({"commit_author": AUTHOR, "min_commits": 1, "commits_since": 1, "commits_until": D + 1}, "Commit range must end by the deadline"),
    ({"min_commits": 3}, "Commit criteria given without commit_author"),
    ({"bogus": 1}, "Unknown milestone field"),
]


@pytest.mark.parametrize("override,msg", BAD_SPECS)
def test_invalid_milestone_spec(direct_vm, grant_contract, direct_owner, direct_alice, override, msg):
    m = milestone(deadline=D)
    m.update(override)
    amount = m["amount"] if isinstance(m["amount"], int) and not isinstance(m["amount"], bool) and m["amount"] > 0 else 0
    with direct_vm.expect_revert(msg):
        create(direct_vm, grant_contract, direct_owner, direct_alice, [milestone(), m], value=GEN + amount)
    assert grant_contract.get_totals()["grant_count"] == 0


def test_milestone_not_object(direct_vm, grant_contract, direct_owner, direct_alice):
    direct_vm.sender = direct_owner
    direct_vm.value = GEN
    with direct_vm.expect_revert("Milestone must be an object"):
        grant_contract.create_grant(to_hex(direct_alice), "pallets", "flask", 7, ["x"])
    direct_vm.value = 0


def test_unknown_grant_and_milestone(direct_vm, grant_contract, direct_owner, direct_alice):
    with direct_vm.expect_revert("Unknown grant"):
        grant_contract.get_grant("1")
    gid = create(direct_vm, grant_contract, direct_owner, direct_alice, [milestone()])
    with direct_vm.expect_revert("Unknown milestone"):
        grant_contract.get_milestone(gid, 1)
    with direct_vm.expect_revert("Unknown milestone"):
        grant_contract.get_milestone(gid, -1)
