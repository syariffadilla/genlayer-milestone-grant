# MilestoneGrant: GitHub-verified milestone grants on GenLayer

A GenLayer Intelligent Contract that escrows a grant or bounty and releases it milestone by milestone.
A milestone is paid when objective, machine-checkable facts about a public GitHub repository are true.
These facts are read from GitHub's public REST API by every validator independently.

* `contracts/milestone_grant.py`: the contract (one file, Python, GenVM)
* `tests/direct/`: 134 direct-mode tests (gltest) built on real captured GitHub responses
* `deploy/studionet.py`: deploy and interaction script (genlayer-py)
* `CONSENSUS.md`: what validators check and why

No frontend. No LLM: the verdict is computed by deterministic code from GitHub data.

## What it does

A **funder** creates a grant for one **grantee** address and one public GitHub repo (`owner/repo`, fixed at
creation). The grant has 1–20 milestones. Each milestone has:

* an amount in wei of GEN
* a deadline (unix seconds, UTC)
* criteria, all of which must hold:
  * **(a) release:** a GitHub release with the exact tag exists. Its `published_at` must be at or before the
    deadline, and at or after `not_before` if that is set.
  * **(b) CI (optional):** the commit the tag points to has passing check runs and/or a combined commit status of
    `success`. You can require all latest check runs, or only a named list.
  * **(c) PR (optional):** a given pull request number is merged into a given base branch at or before the deadline.
  * **(d) activity (optional):** at least N commits by a given GitHub login in a fixed time range (N ≤ 100).

The funder deposits the exact total when creating the grant (`payable`). The grantee **claims** a milestone,
and then **anyone** can call `verify_milestone`:

| Verdict | Effect |
|---|---|
| `PASS` | The milestone amount is sent to the grantee (native transfer), once. Status `PAID`. |
| `FAIL` | No money moves. The milestone goes back to `OPEN`, and the grantee may fix things and claim again before the deadline. |
| `DEFER` | No money moves. Used for a GitHub error, 404, rate limit (403/429), 5xx, malformed JSON, or checks still running. The milestone stays `CLAIMED`, and anyone can retry. |

After `deadline + grace`, the funder can **reclaim** any milestone that is not `PAID`. The funder can **cancel** a
milestone that was never claimed while its deadline is still in the future.

## Why GenLayer

The payout depends on facts that live outside the chain (GitHub). On an ordinary chain you would need a trusted
oracle or a trusted keeper to report them. Here the leader fetches GitHub, and each validator **fetches GitHub again
itself**. A result is accepted only if the validators' own observations match every settlement-driving field.
So no single party's report moves money: a leader that invents or alters a field is voted down.
GitHub responses are not byte-identical between fetches (volatile fields, ordering, rate limits), so the contract
does not compare raw responses. It binds **derived fields**: the exact values that decide the payout.
See [CONSENSUS.md](CONSENSUS.md).

## Bound fields (validator must reproduce every one exactly)

The evidence dict returned by the leader has exactly these keys. A validator recomputes the whole dict from its own
GitHub fetches and accepts only if it is identical, with types compared strictly (canonical JSON, so `true` ≠ `1`).
The validator also checks that the leader's verdict follows from the leader's own facts.

| Field | Source | Meaning |
|---|---|---|
| `grant_id`, `milestone` | contract | which milestone is being settled |
| `owner`, `repo`, `tag`, `deadline` | contract | repo, tag and deadline the facts refer to |
| `checks_mode`, `pr_number`, `commit_author` | contract | which criteria were evaluated |
| `fetch_error` | derived | first failed request, e.g. `GITHUB_RATE_LIMIT:release`, or `""` |
| `release_http`, `release_tag`, `release_draft` | `GET /releases/tags/{tag}` | HTTP status, tag name, draft flag |
| `release_published_at`, `release_published_ts` | same | `published_at` string and its unix seconds (compared with the deadline) |
| `commit_http`, `commit_sha` | `GET /commits/refs/tags/{tag}` | resolved 40-hex commit SHA the tag points to |
| `check_runs_http`, `check_runs_total` | `GET /commits/{sha}/check-runs?filter=latest&per_page=100` | HTTP status and `total_count` |
| `check_runs` | same | sorted list of `[name, status, conclusion]` for every returned run |
| `status_http`, `status_state`, `status_total`, `statuses` | `GET /commits/{sha}/status` | combined state, count, sorted `[context, state]` |
| `pr_http`, `pr_state`, `pr_merged`, `pr_merged_at`, `pr_merged_ts`, `pr_base` | `GET /pulls/{n}` | PR state, merged flag, merge time, base branch |
| `commits_http`, `commits_count` | `GET /commits?author=…&since=…&until=…&per_page=100` | number of commits returned |
| `verdict`, `reason`, `payout` | derived | `PASS`/`FAIL`/`DEFER`, the reason code, and the exact payout in wei (amount or 0) |

Fields that do not decide the payout, such as run timestamps, download counts, reactions and the order of runs in the
response, are not bound. That way, harmless differences between two fetches do not block consensus.

After consensus, the contract re-checks deterministically that the identity fields match the stored milestone, and that
`_evaluate(evidence)` gives the same verdict, reason and payout. Only then does it move money, in integer arithmetic.

## Verdict rules (`_evaluate`, deterministic)

1. If any request failed (`fetch_error` ≠ `""`), the verdict is `DEFER` with that reason. Fetching stops at the first
   failed request.
2. Otherwise each enabled criterion gives `OK`, `FAIL` or `DEFER`:
   * release: `TAG_MISMATCH`, `RELEASE_IS_DRAFT`, `RELEASE_AFTER_DEADLINE`, `RELEASE_BEFORE_NOT_BEFORE` (FAIL).
     `RELEASE_NOT_PUBLISHED` (DEFER).
   * check runs: if `total_count` ≠ the number returned, `CHECKS_TRUNCATED` (DEFER). With no required list, every latest
     run must be `completed` with conclusion `success`, `neutral` or `skipped`, and 0 runs gives `CHECKS_NONE` (FAIL).
     With a required list, only the named runs count, and each must exist (`REQUIRED_CHECK_MISSING:<name>`, FAIL).
     A run that is not completed gives `CHECK_PENDING:<name>` (DEFER). Any other conclusion gives
     `CHECK_FAILED:<name>` (FAIL).
   * combined status: `success` is OK. `pending` gives `STATUS_PENDING` (DEFER). `failure`/`error` give
     `STATUS_FAILURE`/`STATUS_ERROR` (FAIL). 0 statuses give `STATUS_NONE` (FAIL).
   * PR: `PR_NOT_MERGED`, `PR_WRONG_BASE`, `PR_MERGED_AFTER_DEADLINE` (FAIL).
   * commits: fewer than `min_commits` gives `TOO_FEW_COMMITS` (FAIL).
3. The first FAIL wins. If there is no FAIL, the first DEFER wins. If neither, `PASS` with `payout = amount`.

## State machine (per milestone)

```
             claim (grantee, now <= deadline)
   OPEN ─────────────────────────────────────────▶ CLAIMED
    ▲  │                                             │  │
    │  │ cancel (funder, never claimed,              │  │ verify (anyone, now <= deadline+grace)
    │  │         now < deadline)                     │  ├── PASS  ──▶ PAID       (amount → grantee)
    │  ▼                                             │  ├── DEFER ──▶ CLAIMED    (no money moves, retry)
    │ CANCELLED (amount → funder)                    │  └── FAIL  ──▶ OPEN       (no money moves)
    └────────────────────────────────────────────────┘
   OPEN or CLAIMED, now > deadline + grace ── reclaim (funder) ──▶ RECLAIMED (amount → funder)
```

`PAID`, `CANCELLED` and `RECLAIMED` are terminal. Each moves the milestone amount exactly once, and the tests assert this
with captured transfers.

Accounting invariants, checked after every state change in the tests:
`total_locked = total_deposited − total_paid − total_refunded ≥ 0`. Per grant: `locked = deposited − paid − refunded`,
`deposited = Σ amounts`, `paid = Σ amounts of PAID`, `refunded = Σ amounts of CANCELLED/RECLAIMED`, and the sum of
per-grant `locked` equals the global `total_locked`.

## Method reference

| Method | Who | Notes |
|---|---|---|
| `__init__()` | deployer | no arguments |
| `create_grant(grantee: str, owner: str, repo: str, grace_days: int, milestones: list) -> str` | funder, **payable** | `msg.value` must equal the sum of amounts exactly. Returns the grant id (`"1"`, `"2"`, …) |
| `claim_milestone(grant_id: str, index: int)` | grantee | `OPEN` → `CLAIMED`, only while `now ≤ deadline` |
| `verify_milestone(grant_id: str, index: int) -> dict` | anyone | needs `CLAIMED` and `now ≤ deadline + grace`. Returns the evidence dict |
| `cancel_milestone(grant_id: str, index: int) -> int` | funder | never-claimed `OPEN` milestone, `now < deadline`. Refunds the amount |
| `reclaim_milestone(grant_id: str, index: int) -> int` | funder | `OPEN`/`CLAIMED` and `now > deadline + grace`. Refunds the amount |
| `get_grant(grant_id) -> dict` | view | grant with all milestones, `locked` |
| `get_milestone(grant_id, index) -> dict` | view | including `last_verdict`, `last_reason`, and the stored `evidence` JSON |
| `get_totals() -> dict` | view | global accounting |
| `get_static_urls(grant_id, index) -> list` | view | the GitHub URLs that do not depend on the resolved SHA |

Milestone object keys: `amount` (int wei, > 0), `deadline` (int unix s, in the future, ≤ ~3 years ahead), `tag`
(`[A-Za-z0-9._-]`, ≤ 100 chars), `checks` (`none` | `check_runs` | `status` | `both`, default `none`),
`required_checks` (list of names, ≤ 20, only with `check_runs`/`both`), `not_before` (int unix s, default 0),
`pr_number` + `pr_base` (both, or neither), `commit_author` + `min_commits` (1–100) + `commits_since` +
`commits_until` (all, or none; range must end by the deadline). Unknown keys are rejected. `grace_days` is 1–90.

## Units

* Money: wei of native GEN (`int`/`u256`). Integers only, no floats anywhere in money logic.
* Time: unix seconds, UTC (`int`). The contract's "now" is the transaction timestamp (`datetime.now(timezone.utc)`
  inside GenVM). GitHub timestamps (`YYYY-MM-DDTHH:MM:SSZ`) are parsed strictly. Anything else is treated as invalid.

## How to test

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
genvm-lint check contracts/milestone_grant.py
pytest tests/direct
```

`tests/direct/conftest.py` pins the GenVM release that gltest direct mode downloads to `v0.2.16`. You can override it
with `GLTEST_GENVM_VERSION`. Without the pin, gltest asks for GitHub's "latest" GenVM release. That is currently a v0.3
release candidate with no universal tarball (HTTP 404), and every test would error on a fresh machine.

Test files:

* `test_create.py`: deposits, stored criteria, 26 invalid milestone specs, invalid repo, address and grace values,
  insufficient, zero and excess deposits
* `test_verify.py`: PASS (release only, all criteria, exactly at the deadline), double-pay prevention, 15 FAIL cases
  (14 reason codes), 13 DEFER cases (real 403 rate-limit body, 429, real 404, 5xx, bad JSON, bad shape, unreachable, pending,
  truncated), FAIL→re-claim→PASS, DEFER→retry→PASS, and the verification window
* `test_claim_cancel_reclaim.py`: access control, timing edges (exact deadline and grace boundaries), no double refund,
  and a mixed paid/cancelled/reclaimed grant where every wei is accounted for
* `test_validator.py`: the validator agrees on identical data and ignores unbound volatile fields. It rejects 20
  kinds of differing GitHub data, a tampered value in every one of the 35 evidence keys, nested tampering, wrong
  types, extra or missing keys, and a fake PASS on failing data

Fixtures are real GitHub responses for `pallets/flask` release `3.1.3`. See `tests/direct/fixtures/README.md`.

## Deploy

```bash
python deploy/studionet.py faucet --gen 20            # Studio faucet (sim_fundAccount)
python deploy/studionet.py deploy
python deploy/studionet.py create --address <contract> --grantee <0x…> --owner pallets --repo flask \
       --grace-days 7 --milestones "$(cat milestones.json)"
python deploy/studionet.py claim  --address <contract> --grant 1 --index 0 --key-file <grantee key file>
python deploy/studionet.py verify --address <contract> --grant 1 --index 0
python deploy/studionet.py read   --address <contract> --grant 1
```

Keys come from `$GENLAYER_PRIVATE_KEY` or a key file outside the repo (`*.key` is gitignored). Never commit a key.

### Deployed on GenLayer Studionet (what was actually done)

The exact file `contracts/milestone_grant.py` in this repo, sha256
`4ba6e3d29721c2c0f78cc4032afc2b26ce8b2da12fe0f03cb5ed098a40586558`, was deployed on 2026-10-23 (UTC) with throwaway
keys generated for this test. They are not anyone's personal wallet, and Studio balances come from the Studio faucet.

* Contract: [`0x8cD1213d653E5fADADa3B79feBb06fbbF4033211`](https://explorer-studio.genlayer.com/address/0x8cD1213d653E5fADADa3B79feBb06fbbF4033211)
* Funder/deployer: `0x8D0F62AfCc7ff4587363E434eE15cA9f9cAB016b`. Grantee: `0xbD0dc5591fD80D6e48a39Cd0f2170D3d113921b4`

Grant `1` on `pallets/flask`, deadline 2026-11-22 18:21:08 UTC (30 days after creation), grace 7 days:

* milestone 0: 0.1 GEN. Tag `3.1.3` + check runs `build, typing, 3.12, 3.13, Windows, Mac` + combined status + PR
  #5924 merged into `stable` + ≥ 10 commits by `davidism` between 2026-02-01 and 2026-02-20
* milestone 1: 0.05 GEN. Tag `3.1.3` + **all** latest check runs must pass
* milestone 2: 0.02 GEN. Tag `9.0.0` (does not exist), cancelled by the funder

| Step | Tx | Result |
|---|---|---|
| deploy | [`0xe9aa3c06…de03786`](https://explorer-studio.genlayer.com/tx/0xe9aa3c06a20e61d206681812d968dc2c3e5a1054af2687989badab067de03786) | FINALIZED, MAJORITY_AGREE |
| `create_grant` (deposit 0.17 GEN) | [`0x36f64516…e84c6`](https://explorer-studio.genlayer.com/tx/0x36f64516775bfac09bab4397d165abdb9136bda709ecef0e206932bdae0e84c6) | FINALIZED, MAJORITY_AGREE |
| `cancel_milestone(1, 2)` | [`0xa9c9f58e…82db86`](https://explorer-studio.genlayer.com/tx/0xa9c9f58eabf1c9264f1ad96ca1cfbe282c012961da7063fec678be6d9c82db86) | FINALIZED. Transfer message of 0.02 GEN to the funder |
| `claim_milestone(1, 0)` | [`0xe3c33184…aa309`](https://explorer-studio.genlayer.com/tx/0xe3c33184d7fb5146398f43d3807f0822e5ca5dbfc4d340c319fd2ed20d1aa309) | FINALIZED |
| `claim_milestone(1, 1)` | [`0x89242e17…ccad1`](https://explorer-studio.genlayer.com/tx/0x89242e171f08a31209ba7ec9d7736b9c8c819797e885c94b46009687be4ccad1) | FINALIZED |
| `verify_milestone(1, 0)` | [`0x6c060834…76f48`](https://explorer-studio.genlayer.com/tx/0x6c06083499f15aabf0ca69dfd3e1aa7db05db699c7b1f0e088aba30b36276f48) | **FINALIZED, MAJORITY_AGREE** (3 agree, 2 idle). Verdict **PASS**. Transfer message of 0.1 GEN to the grantee |
| `verify_milestone(1, 1)` | [`0x9ae1f04f…724cd4`](https://explorer-studio.genlayer.com/tx/0x9ae1f04f37653ac1bb2faa553aae33537e44bb227f1c32c44ff10207f4724cd4) | **FINALIZED, MAJORITY_AGREE** (3 agree, 2 idle). Verdict **FAIL** `CHECK_FAILED:Development Versions`. No transfer |

In both live verifications, three validators voted `agree`. In this contract a validator can only vote `agree` after
running its own GitHub fetch (`validator_fn` calls `_fetch_evidence`) and getting identical evidence. The stored
evidence matches the captured fixtures field for field: SHA `22d92470…`, 17 check runs, `published_at` 2026-02-19T05:01:30Z, PR merged
2026-02-19T05:01:53Z into `stable`, 15 commits. After these steps, `get_totals()` on the contract reads
deposited 0.17, paid 0.1, refunded 0.02, locked 0.05 GEN, and the Studio balance of the contract address is 0.05 GEN.
Milestone 1 is `OPEN` again and becomes reclaimable by the funder after 2026-11-29 18:21:08 UTC (unless it is claimed and passes first).

The live run covered deploy, create (payable), cancel, claim, verify PASS and verify FAIL. These paths were tested
**only in direct mode**: DEFER (rate limit, 404, 5xx, malformed responses, pending checks), reclaim after
deadline + grace, double-pay attempts, invalid inputs, and validator disagreement.

Before this deployment, an earlier revision of the contract was deployed once as a smoke test
(`0x920D3137220c4902d3a28d365194131A56779cff`, release-only milestone, PASS). Its source differs from the final file,
so it is not the deployment of record.

## Limitations (read before using with real money)

* **Objective signals, not quality.** The criteria show that a tagged release exists, that CI reported success, that a
  PR was merged, and that commits exist. They do not show that the work is good. The repository's maintainers
  control releases, CI configuration and merges. If the grantee is a maintainer, they can influence these signals,
  for example by adding a workflow whose job is named like a required check. Check-run app identity is not bound.
* **Existing history counts.** A release that already exists before the grant is created satisfies (a). Use
  `not_before` (e.g. the creation time) to require a new release. The live demo above deliberately used existing
  history so that a real settlement was possible on the same day.
* **GitHub state can change.** Check runs can be re-run and releases edited or deleted. Verification reads the state at
  verification time, and only timestamps are compared against the deadline.
* **Unauthenticated rate limit.** GitHub allows 60 unauthenticated requests per hour per IP. A verification makes up to
  6 requests per node (leader and each validator). On shared infrastructure this can produce `DEFER`
  (`GITHUB_RATE_LIMIT:*`), or a validator disagreement if only some nodes are limited. In both cases no money moves,
  and the call can be retried within `deadline + grace`. If it never succeeds, the funder reclaims.
* **Pagination.** One page of up to 100 check runs is read. More than that gives `CHECKS_TRUNCATED` (DEFER). The
  commit count reads one page of 100, so `min_commits` is capped at 100. The commit count uses the repo's default
  branch.
* **Disagreement on DEFER reasons.** The validators bind the DEFER reason exactly. If the leader is rate limited but a
  validator gets data (or vice versa), the round does not reach agreement. This is safe (no state change) but costs a
  retry.
* **Studio only.** Tested in gltest direct mode and on GenLayer Studionet. It has not been deployed to a testnet or
  mainnet. Studio balances are simulated: native transfers appear as transaction messages.
* **Funder cancel power.** The funder can cancel any never-claimed milestone before its deadline. A grantee should
  claim as soon as the work is done.
* **SDK version.** Pinned to `py-genlayer:1jb45aa8…` (GenVM v0.2.x). `genvm-lint` reports that a newer runner exists.
  The contract has not been tested against it.

## License

MIT, see [LICENSE](LICENSE).
