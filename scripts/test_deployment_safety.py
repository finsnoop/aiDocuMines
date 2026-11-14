"""Execute deployment scripts with an isolated Docker double; never touch real containers."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.log = self.root / "docker-calls"
        docker = self.root / "docker"
        docker.write_text("""#!/bin/bash
if [ "$1" = info ]; then exit "${FAKE_DOCKER_EXIT:-0}"; fi
if [ "$1" = inspect ]; then
  name="${@: -1}"
  [ "$name" = aidocumines_web ] || exit 1
  case "$*" in
    *State.Status*) echo "${FAKE_STATE:-running}";;
    *State.Health*) echo "${FAKE_HEALTH:-healthy}";;
    *) echo false;;
  esac
  exit 0
fi
printf '%s\n' "$*" >> "$FAKE_LOG"
""")
        docker.chmod(0o700)
        self.env = {**os.environ, "PATH": str(self.root)+os.pathsep+os.environ["PATH"],
                    "FAKE_LOG": str(self.log), "WATCHDOG_STATE_DIR": str(self.root/"state"),
                    "WATCHDOG_PAUSE_FILE": str(self.root/"pause"), "WATCHDOG_UNHEALTHY_RUNS":"3"}
    def run_watchdog(self, **values):
        return subprocess.run(["bash", str(ROOT/"deploy/watchdog.sh")], env={**self.env, **values}, capture_output=True, text=True)
    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []
    def test_unhealthy_restart_requires_three_consecutive_checks(self):
        for _ in range(2): self.assertEqual(self.run_watchdog(FAKE_HEALTH="unhealthy").returncode,0)
        self.assertEqual(self.calls(),[])
        self.run_watchdog(FAKE_HEALTH="unhealthy")
        self.assertEqual(self.calls(),["restart --time 30 aidocumines_web"])
    def test_recovery_clears_unhealthy_counter(self):
        self.run_watchdog(FAKE_HEALTH="unhealthy")
        self.run_watchdog(FAKE_HEALTH="healthy")
        self.run_watchdog(FAKE_HEALTH="unhealthy")
        self.assertEqual(self.calls(),[])
    def test_exited_container_is_started(self):
        self.run_watchdog(FAKE_STATE="exited")
        self.assertEqual(self.calls(),["start aidocumines_web"])
    def test_paused_container_and_operator_pause_are_respected(self):
        self.run_watchdog(FAKE_STATE="paused")
        (self.root/"pause").touch()
        self.run_watchdog(FAKE_STATE="exited")
        self.assertEqual(self.calls(),[])
    def test_unavailable_docker_does_not_issue_restart(self):
        self.assertEqual(self.run_watchdog(FAKE_DOCKER_EXIT="1").returncode,1)
        self.assertEqual(self.calls(),[])
    def test_restart_preflight_refuses_missing_queue_password(self):
        script=self.root/"restart_containers.sh"
        shutil.copy(ROOT/"restart_containers.sh",script)
        (self.root/".env").write_text("SECRET_KEY=test-long-random-key\nPOSTGRES_USER=a\nPOSTGRES_PASSWORD=b\nPOSTGRES_DB=c\nFIELD_ENCRYPTION_KEY=d\nENCRYPTION_SECRET_KEY=e\n")
        result=subprocess.run(["bash",str(script)],env=self.env,capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn("REDIS_PASSWORD is not set",result.stdout)
        self.assertEqual(self.calls(),[])

if __name__ == "__main__": unittest.main(verbosity=2)
