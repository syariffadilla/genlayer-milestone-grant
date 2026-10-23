# Test fixtures: real GitHub REST API responses

Every `.json` file here is an unmodified response body from the public,
unauthenticated GitHub REST API (`api.github.com`). They were captured with `curl` on
2026-10-23 (UTC). The request headers were the ones the contract sends:
`Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`,
`User-Agent: genlayer-milestone-grant`.

Repository: [`pallets/flask`](https://github.com/pallets/flask), release `3.1.3`.

| File | Request | HTTP | Facts the tests rely on |
|---|---|---|---|
| `release_flask_3.1.3.json` | `GET /repos/pallets/flask/releases/tags/3.1.3` | 200 | `tag_name` `3.1.3`, `draft` false, `published_at` `2026-02-19T05:01:30Z` |
| `commit_flask_3.1.3.json` | `GET /repos/pallets/flask/commits/refs/tags/3.1.3` | 200 | `sha` `22d924701a6ae2e4cd01e9a15bbaf3946094af65` (annotated tag peeled to its commit) |
| `check_runs_flask_3.1.3.json` | `GET /repos/pallets/flask/commits/22d92470…/check-runs?filter=latest&per_page=100` | 200 | `total_count` 17. 16 runs `completed`/`success`. `Development Versions` is `completed`/`failure` |
| `status_flask_3.1.3.json` | `GET /repos/pallets/flask/commits/22d92470…/status?per_page=100` | 200 | `state` `success`, `total_count` 1 (`docs/readthedocs.org:flask`) |
| `pull_flask_5924.json` | `GET /repos/pallets/flask/pulls/5924` | 200 | `merged` true, `merged_at` `2026-02-19T05:01:53Z`, `base.ref` `stable`, `state` `closed` |
| `commits_flask_davidism_2026-02.json` | `GET /repos/pallets/flask/commits?author=davidism&since=2026-02-01T00:00:00Z&until=2026-02-20T00:00:00Z&per_page=100` | 200 | 15 commits |
| `release_flask_404.json` | `GET /repos/pallets/flask/releases/tags/99.99.99` | 404 | `{"message":"Not Found",…}` |
| `ratelimit_403.json` | `GET /repos/pallets/flask/releases/tags/3.1.3` from an exhausted IP | 403 | `"API rate limit exceeded for …"`. Response headers are kept in `ratelimit_403.headers.txt` (`x-ratelimit-remaining: 0`) |

The IP address in `ratelimit_403.json` belongs to the sandbox that captured the fixtures. It is not a user address.

## Modified variants (made in memory, never saved here)

Some paths need data that the real responses do not contain: a pending check, a draft
release, an unmerged PR, a 429, a 5xx, malformed JSON. For these, a test loads one of
the real files and changes one field in memory, using `conftest.modified()`. Each test
says which field it changed. The files in this directory are never edited.

The real data covers these outcomes as captured: PASS (release + required checks +
combined status + PR + commit count), FAIL `CHECK_FAILED:Development Versions` (when all
check runs are required), FAIL `PR_WRONG_BASE` (when `main` is required, because #5924 went
into `stable`), FAIL `TOO_FEW_COMMITS` (with `min_commits` 16), DEFER `GITHUB_NOT_FOUND`,
and DEFER `GITHUB_RATE_LIMIT`.

## Re-capturing

```bash
H='-H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28" -H "User-Agent: genlayer-milestone-grant"'
eval curl -s $H -o release_flask_3.1.3.json https://api.github.com/repos/pallets/flask/releases/tags/3.1.3
# … same pattern for the other URLs in the table
```

Live GitHub data can change: check runs can be re-run, and a release can be edited or
deleted. If you re-capture, the reference numbers in `conftest.py` may need updating.
