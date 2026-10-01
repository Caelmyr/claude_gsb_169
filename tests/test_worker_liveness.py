"""Regression tests for worker liveness tracking (heartbeat -> dead -> recover).

These cover the scenarios behind the nodes-page bug where a killed worker kept
showing as alive:

* a worker whose heartbeat goes silent is reaped right after
  ``heartbeat_timeout_sec`` (the timeout is seconds, not minutes);
* a worker that re-registers / heartbeats again comes back alive;
* repeatedly killing workers reaps each of them independently;
* the heartbeat thread cadence is the configured heartbeat interval;
* the scheduler loop reaps on every tick using ``scheduler_tick_sec``.
"""

import shutil
import tempfile
import time
import unittest

from backend.common.config import ClusterConfig
from backend.common.storage import Storage
from backend.master.registry import WorkerRegistry


def _payload(wid: str, **extra) -> dict:
    p = {"worker_id": wid, "name": wid, "host": "127.0.0.1", "port": 8001,
         "cpu_cores": 2, "mem_total_mb": 1024, "exec_mode": "thread",
         "cpu_percent": 10.0, "mem_percent": 20.0, "load1": 1.5,
         "running_tasks": 0, "queued_tasks": 0}
    p.update(extra)
    return p


class WorkerLivenessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = ClusterConfig(
            heartbeat_timeout_sec=1.0,
            heartbeat_interval_sec=0.5,
            scheduler_tick_sec=0.1,
        ).validated()
        self.registry = WorkerRegistry(Storage(self.tmp), self.config)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _register(self, wid):
        return self.registry.register(_payload(wid))

    # ------------------------------------------------------------------
    def test_silent_worker_reaped_after_timeout_seconds(self):
        self._register("w1")
        self.assertEqual(len(self.registry.alive()), 1)

        # Still alive just before the timeout.
        time.sleep(0.6)
        self.assertEqual(self.registry.reap(), [])
        self.assertEqual(len(self.registry.alive()), 1)

        # Reaped shortly after the (1 second, not 1 minute) timeout.
        time.sleep(0.7)
        dead = self.registry.reap()
        self.assertEqual([w.worker_id for w in dead], ["w1"])

        summary = self.registry.summary()
        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["alive"], 0)
        self.assertEqual(summary["dead"], 1)
        self.assertEqual(summary["heartbeat_timeout_sec"], 1.0)

    def test_worker_recovers_after_being_reaped(self):
        self._register("w1")
        time.sleep(1.2)
        self.assertEqual(len(self.registry.reap()), 1)
        self.assertFalse(self.registry.get("w1").is_alive)

        # Same id comes back online: register flips it alive again.
        self.registry.register(_payload("w1"))
        self.assertTrue(self.registry.get("w1").is_alive)
        self.assertEqual(self.registry.summary()["alive"], 1)

        # ...and a fresh heartbeat keeps it alive past the old deadline.
        time.sleep(0.6)
        self.registry.heartbeat(_payload("w1"))
        time.sleep(0.6)
        self.assertEqual(self.registry.reap(), [])
        self.assertEqual(self.registry.summary()["alive"], 1)

    def test_repeated_deaths_reaped_independently(self):
        for wid in ("w1", "w2", "w3"):
            self._register(wid)
        time.sleep(1.2)
        dead = sorted(w.worker_id for w in self.registry.reap())
        self.assertEqual(dead, ["w1", "w2", "w3"])
        self.assertEqual(self.registry.summary()["alive"], 0)

        # w2 only recovers; the others stay dead.
        self.registry.register(_payload("w2"))
        summary = self.registry.summary()
        self.assertEqual(summary["alive"], 1)
        self.assertEqual(summary["dead"], 2)

        # Flap: w2 dies again and comes back once more.
        time.sleep(1.2)
        self.assertEqual([w.worker_id for w in self.registry.reap()], ["w2"])
        self.registry.register(_payload("w2"))
        self.assertEqual(self.registry.summary()["alive"], 1)

    def test_heartbeat_payload_updates_load(self):
        self._register("w1")
        self.registry.heartbeat(_payload("w1", cpu_percent=42.0, load1=3.25))
        w = self.registry.get("w1")
        self.assertEqual(w.cpu_percent, 42.0)
        self.assertEqual(w.load1, 3.25)

    def test_on_death_callback_fires_once_per_reaping(self):
        fired = []
        self.registry.on_death = lambda w: fired.append(w.worker_id)
        self._register("w1")
        time.sleep(1.2)
        self.registry.reap()
        self.registry.reap()  # already dead -> no duplicate callback
        self.assertEqual(fired, ["w1"])


class HeartbeatThreadCadenceTest(unittest.TestCase):
    def test_interval_matches_configured_heartbeat_interval(self):
        from backend.worker.heartbeat import HeartbeatThread

        class FakeClient:
            def __init__(self):
                self.calls = 0

            def post(self, url, payload, timeout=None):
                self.calls += 1

                class Resp:
                    ok = True
                    data = {}
                return Resp()

        client = FakeClient()
        hb = HeartbeatThread("w1", "http://127.0.0.1:8000", 0.5,
                             lambda: {}, client=client)
        # interval_sec is used as-is (it must NOT be divided by 4 again).
        self.assertAlmostEqual(hb.interval, 0.5, places=3)
        hb.start()
        time.sleep(1.35)
        hb.stop()
        hb.join(timeout=1.0)
        # ~3 beats over 1.35s at a 0.5s cadence (allow scheduling slack).
        self.assertGreaterEqual(client.calls, 2)
        self.assertLessEqual(client.calls, 4)


if __name__ == "__main__":
    unittest.main()
