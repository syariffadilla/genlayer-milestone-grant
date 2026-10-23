"""Deploy / interact with MilestoneGrant on GenLayer Studionet using genlayer-py.

  python deploy/studionet.py deploy
  python deploy/studionet.py create --address 0x... --grantee 0x... --owner pallets --repo flask \
        --grace-days 7 --milestones '[{"amount": 100000000000000000, "deadline": 1793750400, "tag": "3.1.3", "checks": "check_runs"}]'
  python deploy/studionet.py claim  --address 0x... --grant 1 --index 0 --key-file <grantee key>
  python deploy/studionet.py verify --address 0x... --grant 1 --index 0
  python deploy/studionet.py tx     --hash 0x...
  python deploy/studionet.py read   --address 0x... [--grant 1]

Keys are read from $GENLAYER_PRIVATE_KEY or from --key-file (a fresh random key
is created there if the file does not exist). Keep key files outside the repo.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from eth_account import Account
from genlayer_py import create_client
from genlayer_py.chains import studionet
from genlayer_py.types import TransactionStatus

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts" / "milestone_grant.py"
GEN = 10**18


def load_account(key_file: str):
    key = os.environ.get("GENLAYER_PRIVATE_KEY")
    if not key:
        p = Path(key_file)
        if not p.exists():
            acct = Account.create()
            p.write_text(acct.key.hex())
            os.chmod(p, 0o600)
            print(f"created new key at {p}")
        key = p.read_text().strip()
    return Account.from_key(key)


def wait(client, tx_hash, status=TransactionStatus.ACCEPTED, retries=100):
    return client.wait_for_transaction_receipt(transaction_hash=tx_hash, status=status, interval=3000, retries=retries)


def summary(r) -> str:
    keys = ("hash", "status_name", "result_name", "to_address", "num_of_rounds")
    return json.dumps({k: str(r.get(k)) for k in keys if k in r}, indent=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["faucet", "deploy", "create", "claim", "verify", "tx", "read"])
    ap.add_argument("--key-file", default=str(ROOT.parent / ".studionet-deployer-milestone.key"))
    ap.add_argument("--address")
    ap.add_argument("--grantee")
    ap.add_argument("--owner")
    ap.add_argument("--repo")
    ap.add_argument("--grace-days", type=int, default=7)
    ap.add_argument("--milestones", help="JSON list of milestone specs (amounts in wei, deadlines in unix seconds)")
    ap.add_argument("--grant")
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("--hash")
    ap.add_argument("--gen", type=float, default=10)
    ap.add_argument("--finalized", action="store_true", help="wait for FINALIZED instead of ACCEPTED")
    a = ap.parse_args()

    account = load_account(a.key_file)
    client = create_client(chain=studionet, account=account)
    print("account", account.address)
    target = TransactionStatus.FINALIZED if a.finalized else TransactionStatus.ACCEPTED

    if a.cmd == "faucet":
        print(client.fund_account(address=account.address, amount=int(a.gen * GEN)).hex())
        print("balance", client.get_balance(account.address))
    elif a.cmd == "deploy":
        tx = client.deploy_contract(code=CONTRACT.read_text(), args=[])
        print("deploy tx", tx)
        r = wait(client, tx, target)
        data = r.get("data") or {}
        print("contract", data.get("contract_address") or r.get("to_address") or r.get("recipient"))
        print(summary(r))
    elif a.cmd == "create":
        ms = json.loads(a.milestones)
        total = sum(int(m["amount"]) for m in ms)
        tx = client.write_contract(
            address=a.address,
            function_name="create_grant",
            args=[a.grantee, a.owner, a.repo, a.grace_days, ms],
            value=total,
        )
        print("create tx", tx, "deposit", total)
        print(summary(wait(client, tx, target)))
    elif a.cmd == "claim":
        tx = client.write_contract(address=a.address, function_name="claim_milestone", args=[a.grant, a.index])
        print("claim tx", tx)
        print(summary(wait(client, tx, target)))
    elif a.cmd == "verify":
        tx = client.write_contract(address=a.address, function_name="verify_milestone", args=[a.grant, a.index])
        print("verify tx", tx, flush=True)
        print(summary(wait(client, tx, target, retries=200)))
    elif a.cmd == "tx":
        t = client.get_transaction(transaction_hash=a.hash)
        print(json.dumps({k: str(v)[:3000] for k, v in t.items()}, indent=1, default=str))
    elif a.cmd == "read":
        print(json.dumps(client.read_contract(address=a.address, function_name="get_totals"), indent=1, default=str))
        if a.grant:
            print(json.dumps(client.read_contract(address=a.address, function_name="get_grant", args=[a.grant]), indent=1, default=str))


if __name__ == "__main__":
    sys.exit(main())
