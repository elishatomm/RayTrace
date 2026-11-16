"""Isolated design spike, not the Recovery Workbench application or a payment-gateway SDK.

All records and secrets are synthetic. Python standard library only.
Run: python3 experiments/feasibility_spike.py
Writes spike results to verification_results.json next to this file.
"""
from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import math
import platform
import random
import resource
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SECRET = b"synthetic-fixture-secret-not-a-provider-credential"


def signature(raw: bytes) -> str:
    return hmac.new(SECRET, raw, hashlib.sha256).hexdigest()


def initialize(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
            CREATE TABLE inbox (
                run TEXT, event_id TEXT, digest TEXT NOT NULL, raw BLOB NOT NULL,
                PRIMARY KEY (run, event_id));
            CREATE TABLE fulfilments (
                run TEXT, order_id TEXT, payment_id TEXT, amount INTEGER,
                PRIMARY KEY (run, order_id));
        """)


def receive(path: Path, run: str, event_id: str, raw: bytes, sig: str,
            crash_before_commit: bool = False) -> str:
    if not hmac.compare_digest(signature(raw), sig):
        return "rejected_signature"
    # Verify original bytes before parsing. No side effects for malformed bodies.
    try:
        obj = json.loads(raw)
        if not isinstance(obj, dict) or "payment_id" not in obj:
            return "rejected_schema"
    except (ValueError, TypeError):
        return "rejected_schema"
    digest = hashlib.sha256(raw).hexdigest()
    with sqlite3.connect(path, timeout=10) as db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute(
            "SELECT digest FROM inbox WHERE run=? AND event_id=?", (run, event_id)
        ).fetchone()
        if previous:
            return "duplicate" if previous[0] == digest else "conflicting_id"
        db.execute("INSERT INTO inbox VALUES (?,?,?,?)", (run, event_id, digest, raw))
        if crash_before_commit:
            raise RuntimeError("injected storage transaction failure")
    return "accepted"


def fulfil(path: Path, run: str, order_id: str, payment_id: str,
           status: str, amount: int, expected: int, currency: str = "INR",
           captures_for_order: int = 1) -> str:
    if status != "captured":
        return "blocked_state"
    if amount != expected or amount <= 0 or currency != "INR":
        return "blocked_amount_currency"
    if captures_for_order != 1:
        return "manual_review"
    with sqlite3.connect(path, timeout=10) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT payment_id FROM fulfilments WHERE run=? AND order_id=?", (run, order_id)
        ).fetchone()
        if row:
            return "already_fulfilled" if row[0] == payment_id else "manual_review"
        db.execute("INSERT INTO fulfilments VALUES (?,?,?,?)", (run, order_id, payment_id, amount))
    return "fulfilled"


def reduce_state(events: list[dict], as_of: int) -> dict:
    # This restricted reducer covers only created/failed/authorized/captured.
    # Known simplification: ties on occurred_at fall back to event_id order, so a
    # same-time failed/authorized pair can set or clear the conflict flag depending
    # on IDs. The application must treat such pairs as a conflict (SPEC.md section 7).
    visible = [e for e in events if e["known_at"] <= as_of and e["occurred_at"] <= as_of]
    visible.sort(key=lambda e: (e["occurred_at"], e["event_id"]))
    state = "unknown"
    conflict = False
    for e in visible:
        incoming = e["status"]
        if incoming == "captured":
            state = "captured"
        elif incoming == "authorized":
            if state != "captured":
                state = "authorized"
        elif incoming == "failed":
            if state in {"authorized", "captured"}:
                conflict = True
            else:
                state = "failed"
        elif incoming == "created" and state == "unknown":
            state = "created"
    return {"status": state, "needs_reconciliation": conflict}


def event(kind: str, occurred: int, known: int, name: str = "") -> dict:
    return {"status": kind, "occurred_at": occurred, "known_at": known,
            "event_id": name or f"evt_{kind}_{occurred}"}


def approval_allowed(plan: dict, approval: dict, current: dict, now: int) -> bool:
    return all([
        approval.get("actor") == "human",
        approval.get("plan_hash") == plan["hash"],
        approval.get("run_id") == plan["run_id"] == current["run_id"],
        approval.get("expires_at", 0) > now,
        plan["evidence_version"] == current["evidence_version"],
        current["snapshot_age"] <= 30,
        current["status"] == "captured",
        current["captures_for_order"] == 1,
        current["worker_healthy"],
        not current["cancelled"],
        not current["refunded"],
    ])


def wilson(success: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if not total:
        return (0.0, 1.0)
    p = success / total
    d = 1 + z * z / total
    mid = (p + z * z / (2 * total)) / d
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / d
    return mid - half, mid + half


def degraded(base_s: int, base_n: int, now_s: int, now_n: int) -> bool:
    # A heuristic spike threshold, not a calibrated sequential hypothesis test.
    if base_n < 200 or now_n < 50:
        return False
    return (base_s / base_n - now_s / now_n >= .10
            and wilson(now_s, now_n)[1] < wilson(base_s, base_n)[0])


def capture_total(attempts: list[dict]) -> int:
    # Known simplification: refunds are netted into the capture total here. The
    # application must report captures and refunds separately (EVALUATION.md section 8).
    unique = {a["payment_id"]: a for a in attempts}
    return sum(a["amount"] - a.get("refunded", 0) for a in unique.values() if a["status"] == "captured")


def validate_tool(name: str, arguments: dict) -> bool:
    schemas = {"get_payment": {"payment_id"}, "get_worker_health": set()}
    return name in schemas and set(arguments) == schemas[name]


class DesignTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="workbench_spike_")
        self.db = Path(self.tmp.name) / "events.sqlite"
        initialize(self.db)
        self.raw = b'{"payment_id":"pay_synth_1", "amount":120000}'

    def tearDown(self):
        self.tmp.cleanup()

    def test_01_valid_raw_signature(self):
        self.assertEqual(receive(self.db, "r", "e", self.raw, signature(self.raw)), "accepted")

    def test_02_mutated_raw_body_rejected(self):
        changed = json.dumps(json.loads(self.raw), separators=(",", ":")).encode()
        self.assertNotEqual(self.raw, changed)
        self.assertEqual(receive(self.db, "r", "e", changed, signature(self.raw)), "rejected_signature")

    def test_03_invalid_signature_has_no_db_effect(self):
        receive(self.db, "r", "e", self.raw, "bad")
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0], 0)

    def test_04_malformed_signed_payload_rejected(self):
        raw = b'{"not_a_payment": 1}'
        self.assertEqual(receive(self.db, "r", "e", raw, signature(raw)), "rejected_schema")

    def test_05_concurrent_duplicate_receipts(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: receive(self.db, "r", "e", self.raw, signature(self.raw)), range(32)))
        self.assertEqual(results.count("accepted"), 1)
        self.assertEqual(results.count("duplicate"), 31)

    def test_06_conflicting_event_id(self):
        receive(self.db, "r", "e", self.raw, signature(self.raw))
        raw2 = b'{"payment_id":"pay_synth_2"}'
        self.assertEqual(receive(self.db, "r", "e", raw2, signature(raw2)), "conflicting_id")

    def test_07_different_events_one_business_effect(self):
        self.assertEqual(fulfil(self.db, "r", "o", "p", "captured", 100, 100), "fulfilled")
        self.assertEqual(fulfil(self.db, "r", "o", "p", "captured", 100, 100), "already_fulfilled")

    def test_08_concurrent_fulfilment(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            result = list(pool.map(lambda _: fulfil(self.db, "r", "o", "p", "captured", 100, 100), range(32)))
        self.assertEqual(result.count("fulfilled"), 1)

    def test_09_state_order_permutations(self):
        ev = [event("created", 1, 1), event("authorized", 2, 4), event("captured", 3, 3)]
        for permutation in itertools.permutations(ev):
            self.assertEqual(reduce_state(list(permutation), 5)["status"], "captured")

    def test_10_late_authorization(self):
        ev = [event("failed", 10, 11), event("authorized", 20, 21)]
        self.assertEqual(reduce_state(ev, 15)["status"], "failed")
        self.assertEqual(reduce_state(ev, 22)["status"], "authorized")

    def test_11_as_of_excludes_future_knowledge(self):
        ev = [event("created", 1, 1), event("captured", 5, 50)]
        self.assertEqual(reduce_state(ev, 20)["status"], "created")

    def test_12_contradiction_requests_reconciliation(self):
        result = reduce_state([event("captured", 3, 3), event("failed", 4, 4)], 5)
        self.assertEqual(result, {"status": "captured", "needs_reconciliation": True})

    def test_13_authorized_does_not_fulfil(self):
        self.assertEqual(fulfil(self.db, "r", "o", "p", "authorized", 100, 100), "blocked_state")

    def test_14_amount_or_currency_mismatch(self):
        self.assertEqual(fulfil(self.db, "r", "o", "p", "captured", 1, 100), "blocked_amount_currency")
        self.assertEqual(fulfil(self.db, "r", "o", "p", "captured", 100, 100, "USD"), "blocked_amount_currency")

    def test_15_multiple_captures_review(self):
        self.assertEqual(fulfil(self.db, "r", "o", "p", "captured", 100, 100, captures_for_order=2), "manual_review")

    def test_16_run_isolation(self):
        for run in ("a", "b"):
            self.assertEqual(receive(self.db, run, "e", self.raw, signature(self.raw)), "accepted")

    def test_17_transaction_failure_rolls_back(self):
        with self.assertRaises(RuntimeError):
            receive(self.db, "r", "e", self.raw, signature(self.raw), crash_before_commit=True)
        self.assertEqual(receive(self.db, "r", "e", self.raw, signature(self.raw)), "accepted")

    def test_18_approval_rechecks_state(self):
        plan = {"hash": "p1", "run_id": "r", "evidence_version": 2}
        approval = {"actor": "human", "plan_hash": "p1", "run_id": "r", "expires_at": 200}
        current = {"run_id": "r", "evidence_version": 2, "snapshot_age": 2, "status": "captured",
                   "captures_for_order": 1, "worker_healthy": True, "cancelled": False, "refunded": False}
        self.assertTrue(approval_allowed(plan, approval, current, 100))
        for key, value in [("evidence_version", 3), ("snapshot_age", 31), ("status", "failed"),
                           ("captures_for_order", 2), ("worker_healthy", False), ("cancelled", True),
                           ("refunded", True), ("run_id", "another")]:
            self.assertFalse(approval_allowed(plan, approval, {**current, key: value}, 100))
        self.assertFalse(approval_allowed(plan, {**approval, "actor": "llm"}, current, 100))
        self.assertFalse(approval_allowed(plan, {**approval, "plan_hash": "changed"}, current, 100))
        self.assertFalse(approval_allowed(plan, approval, current, 200))

    def test_19_low_sample_suppression(self):
        self.assertFalse(degraded(950, 1000, 2, 3))

    def test_20_large_observed_drop_detected(self):
        self.assertTrue(degraded(950, 1000, 25, 100))

    def test_21_mix_shift_not_within_cohort_degradation(self):
        # Overall rate drops 0.904 -> 0.676 while both component rates stay fixed.
        self.assertTrue(degraded(904, 1000, 676, 1000))
        self.assertFalse(degraded(784, 800, 196, 200))
        self.assertFalse(degraded(120, 200, 480, 800))

    def test_22_restored_processing_is_not_new_capture(self):
        attempt = {"payment_id": "p", "amount": 120000, "status": "captured"}
        before = capture_total([attempt])
        self.assertEqual(fulfil(self.db, "r", "o", "p", "captured", 120000, 120000), "fulfilled")
        after = capture_total([attempt, attempt])
        self.assertEqual(after - before, 0)
        new_attempt = {"payment_id": "p2", "amount": 30000, "status": "captured", "refunded": 1000}
        self.assertEqual(capture_total([attempt, attempt, new_attempt]) - before, 29000)

    def test_23_unknown_fields_do_not_change_policy(self):
        self.assertTrue(validate_tool("get_payment", {"payment_id": "p"}))
        self.assertFalse(validate_tool("refund_all", {}))
        self.assertFalse(validate_tool("get_payment", {"payment_id": "p", "run_id": "other"}))
        self.assertFalse(validate_tool("get_payment", {"payment_id": "p", "approval": "human"}))


def benchmark() -> dict:
    rng = random.Random(20261111)
    started = time.perf_counter()
    events_processed = 0
    successes = 0
    # All 100k cases are transient; the benchmark does not prove DB/API throughput.
    for i in range(100_000):
        success = rng.random() < .9
        ev = [event("created", 1, 1, f"c{i}"),
              event("captured" if success else "failed", 3, 4, f"f{i}")]
        if success:
            ev.append(event("authorized", 2, 5, f"a{i}"))
        rng.shuffle(ev)
        successes += reduce_state(ev, 6)["status"] == "captured"
        events_processed += len(ev)
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_kib = rss / 1024 if platform.system() == "Darwin" else rss
    return {"attempts": 100000, "events_processed": events_processed,
            "captured": successes, "seed": 20261111,
            "seconds": round(time.perf_counter() - started, 4),
            "process_max_rss_kib": rss_kib,
            "scope": "in-process synthetic reducer only; excludes SQL, HTTP, LLM and UI"}


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(DesignTests))
    output = {"date": "2026-11-11", "python": platform.python_version(), "platform": platform.system(),
              "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
              "passed": result.wasSuccessful(), "benchmark": benchmark(),
              "not_tested": ["local LLM", "production payment-gateway API", "real-money operations",
                             "browser UI", "public hosting", "full detector or causal accuracy"]}
    Path(__file__).with_name("verification_results.json").write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps(output, indent=2))
    raise SystemExit(0 if result.wasSuccessful() else 1)
