"""Execute deployment scripts with an isolated Docker double; never touch real containers."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        deploy = self.root / "deploy"
        deploy.mkdir()
        self.watchdog = deploy / "watchdog.sh"
        self.watchdog.write_text((ROOT / "deploy/watchdog.sh").read_text(), newline="\n")
        (deploy / "recovery-lock.sh").write_text((ROOT / "deploy/recovery-lock.sh").read_text(), newline="\n")
        self.log = self.root / "docker-calls"
        docker = self.root / "docker"
        docker.write_text(r"""#!/bin/bash
if [ "$1" = info ]; then exit "${FAKE_DOCKER_EXIT:-0}"; fi
if [ "$1" = image ] && [ "$2" = inspect ]; then exit 0; fi
if [ "$1" = compose ]; then
  printf '%s\n' "$*" >> "$FAKE_LOG"
  case " $* " in
    *' config '*)
      printf 'name: %s\n' "${FAKE_COMPOSE_PROJECT-diagnostic-project}"
      exit "${FAKE_COMPOSE_CONFIG_EXIT:-${FAKE_COMPOSE_EXIT:-0}}";;
    *' build '*) exit "${FAKE_COMPOSE_BUILD_EXIT:-${FAKE_COMPOSE_EXIT:-0}}";;
    *' up '*) exit "${FAKE_COMPOSE_UP_EXIT:-${FAKE_COMPOSE_EXIT:-0}}";;
    *) exit "${FAKE_COMPOSE_EXIT:-0}";;
  esac
fi
if [ "$1" = inspect ]; then
  name="${@: -1}"
  case "$*" in *com.docker.compose.project*) exit 0;; esac
  state="${FAKE_STATE:-running}"; health="${FAKE_HEALTH:-healthy}"; code=0; oom=false
  if [ -n "${FAKE_METADATA_HANDOFF:-}" ]; then
    case "$name" in
      aidocumines_milvus) state="${FAKE_MILVUS_STATE:-exited}"; health="${FAKE_MILVUS_HEALTH:-unhealthy}";;
      aidocumines_milvus_etcd) state="${FAKE_METADATA_STATE:-running}"; health=healthy;;
      aidocumines_web) state=exited; health=none;;
      *) exit 1;;
    esac
  elif [ "${FAKE_DEPLOYMENT_FAILURE:-0}" = 1 ]; then
    case "$name" in
      aidocumines_milvus) state=restarting; health=unhealthy; code=134;;
      aidocumines_api_layer_db|aidocumines_web) state=running; health=healthy;;
      aidocumines_db_prepare) state=exited; health=none;;
      *) exit 1;;
    esac
  elif [ "$name" != "${FAKE_CONTAINER:-aidocumines_web}" ]; then
    exit 1
  fi
  case "$*" in
    *ETCD_USE_EMBED*)
      case "${FAKE_METADATA_HANDOFF:-embedded}" in
        external) echo ETCD_USE_EMBED=false;;
        missing) :;;
        *) echo ETCD_USE_EMBED=true;;
      esac;;
    *State.Status*) echo "$state";;
    *State.Health*) echo "$health";;
    *State.ExitCode*) echo "$code";;
    *State.OOMKilled*) echo "$oom";;
    *) echo false;;
  esac
  exit 0
fi
printf '%s\n' "$*" >> "$FAKE_LOG"
if [ "$1" = start ] || [ "$1" = restart ]; then
  if [ "${FAKE_RECOVERY_EXIT:-0}" != 0 ]; then
    printf '%s\n' "${FAKE_RECOVERY_ERROR:-Error response from daemon: recovery failed}" >&2
  fi
  exit "${FAKE_RECOVERY_EXIT:-0}"
fi
""", newline="\n")
        docker.chmod(0o700)
        self.env = {**os.environ, "PATH": str(self.root)+os.pathsep+os.environ["PATH"],
                    "FAKE_LOG": str(self.log), "WATCHDOG_STATE_DIR": str(self.root/"state"),
                    "WATCHDOG_PAUSE_FILE": str(self.root/"pause"), "WATCHDOG_UNHEALTHY_RUNS":"3",
                    "AIDOCUMINES_RECOVERY_LOCK_FILE": str(self.root / "recovery.lock")}
    def run_watchdog(self, **values):
        return subprocess.run(["bash", str(self.watchdog)], env={**self.env, **values}, capture_output=True, text=True)
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
    def test_exited_ollama_is_started(self):
        result = self.run_watchdog(FAKE_CONTAINER="aidocumines_ollama", FAKE_STATE="exited")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.calls(), ["start aidocumines_ollama"])
    def test_exited_milvus_metadata_service_is_started(self):
        result = self.run_watchdog(FAKE_CONTAINER="aidocumines_milvus_etcd", FAKE_STATE="exited")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.calls(), ["start aidocumines_milvus_etcd"])
    def test_embedded_milvus_without_external_container_still_recovers(self):
        result = self.run_watchdog(FAKE_CONTAINER="aidocumines_milvus", FAKE_STATE="exited")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.calls(), ["start aidocumines_milvus"])
    def test_partial_metadata_handoff_never_starts_embedded_milvus_but_recovers_other_containers(self):
        for mode in ["embedded", "missing"]:
            with self.subTest(mode=mode):
                if self.log.exists(): self.log.unlink()
                result = self.run_watchdog(FAKE_METADATA_HANDOFF=mode)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("metadata handoff incomplete; resume deployment/manual review", result.stdout)
                self.assertEqual(self.calls(), ["start aidocumines_web"])
    def test_partial_metadata_handoff_never_starts_external_etcd_or_restarts_embedded_milvus(self):
        result = self.run_watchdog(FAKE_METADATA_HANDOFF="embedded", FAKE_MILVUS_STATE="running",
                                   FAKE_MILVUS_HEALTH="unhealthy", FAKE_METADATA_STATE="exited", WATCHDOG_UNHEALTHY_RUNS="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ["start aidocumines_web"])
    def test_completed_external_metadata_handoff_recovers_both_services(self):
        result = self.run_watchdog(FAKE_METADATA_HANDOFF="external", FAKE_METADATA_STATE="exited")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("metadata handoff incomplete", result.stdout)
        self.assertEqual(self.calls(), ["start aidocumines_milvus_etcd", "start aidocumines_milvus", "start aidocumines_web"])
    def test_failed_start_retains_bounded_docker_error(self):
        result = self.run_watchdog(FAKE_STATE="exited", FAKE_RECOVERY_EXIT="1",
                                   FAKE_RECOVERY_ERROR="x" * 5000 + "\nlast daemon failure")
        self.assertEqual(result.returncode, 0)  # the watchdog continues checking other containers
        self.assertIn("could not start aidocumines_web (exit 1)", result.stdout)
        self.assertIn("last daemon failure", result.stdout)
        self.assertLess(len(result.stdout), 2500)
    def test_failed_restart_reports_error_and_preserves_retry_counter(self):
        result = self.run_watchdog(FAKE_HEALTH="unhealthy", WATCHDOG_UNHEALTHY_RUNS="1",
                                   FAKE_RECOVERY_EXIT="1", FAKE_RECOVERY_ERROR="daemon restart rejected")
        self.assertEqual(result.returncode, 0)
        self.assertIn("could not restart aidocumines_web (exit 1): daemon restart rejected", result.stdout)
        counter = self.root / "state" / "aidocumines_web.unhealthy"
        self.assertEqual(counter.read_text().strip(), "1")
        self.run_watchdog(FAKE_HEALTH="unhealthy", WATCHDOG_UNHEALTHY_RUNS="1")
        self.assertFalse(counter.exists())
    def test_paused_container_and_operator_pause_are_respected(self):
        self.run_watchdog(FAKE_STATE="paused")
        (self.root/"pause").touch()
        self.run_watchdog(FAKE_STATE="exited")
        self.assertEqual(self.calls(),[])
    def test_unavailable_docker_does_not_issue_restart(self):
        self.assertEqual(self.run_watchdog(FAKE_DOCKER_EXIT="1").returncode,1)
        self.assertEqual(self.calls(),[])
    def test_busy_recovery_lock_skips_docker_and_all_container_recovery(self):
        import fcntl
        with (self.root / "recovery.lock").open("w") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_watchdog(FAKE_DOCKER_EXIT="1", FAKE_STATE="exited")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("another recovery operation holds the lock", result.stdout)
        self.assertEqual(self.calls(), [])
    def test_unusable_recovery_lock_fails_before_container_recovery(self):
        not_directory = self.root / "not-a-directory"
        not_directory.touch()
        result = self.run_watchdog(AIDOCUMINES_RECOVERY_LOCK_FILE=str(not_directory / "recovery.lock"), FAKE_STATE="exited")
        self.assertEqual(result.returncode, 1)
        self.assertIn("could not acquire the recovery lock", result.stdout)
        self.assertEqual(self.calls(), [])
    def test_restart_preflight_refuses_missing_queue_password(self):
        script=self.root/"restart_containers.sh"
        script.write_text((ROOT/"restart_containers.sh").read_text(), newline="\n")
        (self.root/".env").write_text("SECRET_KEY=test-long-random-key\nPOSTGRES_USER=a\nPOSTGRES_PASSWORD=b\nPOSTGRES_DB=c\nFIELD_ENCRYPTION_KEY=d\nENCRYPTION_SECRET_KEY=e\n")
        result=subprocess.run(["bash",str(script)],env=self.env,capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn("REDIS_PASSWORD is not set",result.stdout)
        self.assertEqual(self.calls(),[])

    REQUIRED = ["SECRET_KEY", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "FIELD_ENCRYPTION_KEY",
                "ENCRYPTION_SECRET_KEY", "REDIS_PASSWORD", "ELASTICSEARCH_PASSWORD", "ONLYOFFICE_JWT_SECRET",
                "OO_POSTGRES_DB", "OO_POSTGRES_USER", "OO_POSTGRES_PASSWORD"]

    def run_restart(self, env_text, **values):
        script = self.root/"restart_containers.sh"
        script.write_text((ROOT/"restart_containers.sh").read_text(), newline="\n")
        # Metadata preparation is tested separately; this fixture isolates restart preflight and reporting.
        deploy = self.root / "deploy"
        deploy.mkdir(exist_ok=True)
        (deploy / "prepare-milvus-etcd.sh").write_text(
            '#!/bin/bash\nprintf "%s\\n" metadata-prepare >> "$FAKE_LOG"\n'
            'printf "metadata-project:%s\\n" "${AIDOCUMINES_COMPOSE_PROJECT:-unset}" >> "$FAKE_LOG"\n'
            'exit "${FAKE_PREPARE_EXIT:-0}"\n', newline="\n")
        (self.root/".env").write_text(env_text)
        return subprocess.run(["bash", str(script)], env={**self.env, **values}, cwd=self.root,
                              capture_output=True, text=True, timeout=120)

    def test_restart_preflight_names_every_missing_variable(self):
        result = self.run_restart("SECRET_KEY=test-long-random-key\n")
        self.assertNotEqual(result.returncode, 0)
        for name in self.REQUIRED[1:]:
            self.assertIn(f"{name} is not set", result.stdout)
        self.assertNotIn("n is not set", result.stdout.replace("_n is", ""))   # no stray tokens in the list
        self.assertEqual(self.calls(), [])

    def test_restart_preflight_refuses_django_insecure_key(self):
        env = "".join(f"{k}=value-{i}\n" for i, k in enumerate(self.REQUIRED)).replace(
            "SECRET_KEY=value-0", "SECRET_KEY=django-insecure-abc")
        result = self.run_restart(env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("placeholder", result.stdout)
        self.assertEqual(self.calls(), [])

    def test_restart_preflight_passes_with_every_variable_set(self):
        env = "".join(f"{k}=value-{i}\n" for i, k in enumerate(self.REQUIRED))
        result = self.run_restart(env, FAKE_COMPOSE_EXIT="1")   # stop at the build: nothing real is touched
        self.assertNotIn("is not set", result.stdout)
        self.assertNotIn("placeholder", result.stdout)
        self.assertTrue(any(c.startswith("compose") and "build" in c for c in self.calls()), result.stdout)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Nothing was restarted", result.stdout)

    def test_failed_compose_up_names_milvus_without_blaming_healthy_database_or_migration(self):
        env = "".join(f"{k}=value-{i}\n" for i, k in enumerate(self.REQUIRED))
        result = self.run_restart(env, FAKE_COMPOSE_UP_EXIT="7", FAKE_DEPLOYMENT_FAILURE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("aidocumines_milvus: status=restarting health=unhealthy exit=134 oom=false", result.stdout)
        self.assertIn("Inspect logs: docker logs --tail 120 aidocumines_milvus", result.stdout)
        self.assertNotIn("Inspect logs: docker logs --tail 120 aidocumines_api_layer_db", result.stdout)
        self.assertNotIn("aidocumines_db_prepare: status=", result.stdout)
        self.assertTrue(any(" build" in call for call in self.calls()))
        self.assertTrue(any(" up " in call for call in self.calls()))
        self.assertTrue(any(call.endswith(" ps -a") for call in self.calls()))
        self.assertFalse(any(call.startswith(("start ", "restart ")) for call in self.calls()))

    def test_metadata_preparation_failure_prevents_compose_up_or_restart(self):
        env = "".join(f"{k}=value-{i}\n" for i, k in enumerate(self.REQUIRED))
        result = self.run_restart(env, FAKE_PREPARE_EXIT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("metadata-prepare", self.calls())
        self.assertTrue(any(" build" in call for call in self.calls()))
        self.assertFalse(any(" up " in call for call in self.calls()))
        self.assertFalse(any(call.startswith(("start ", "restart ")) for call in self.calls()))

    def test_fresh_install_resolves_compose_project_and_passes_it_to_metadata_preparation(self):
        env = "".join(f"{k}=value-{i}\n" for i, k in enumerate(self.REQUIRED))
        result = self.run_restart(env, FAKE_COMPOSE_PROJECT="fresh-deployment", FAKE_PREPARE_EXIT="1")
        self.assertNotEqual(result.returncode, 0)  # stop at the isolated preparation double
        self.assertTrue(any(call.endswith(" config --format yaml") for call in self.calls()))
        self.assertIn("metadata-project:fresh-deployment", self.calls())
        self.assertNotIn("Could not resolve the Compose project identity", result.stdout)
        self.assertFalse(any(" up " in call for call in self.calls()))

class WatchdogInstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.unit_dir = self.root / "units"
        self.unit_dir.mkdir()
        deploy = self.root / "deploy"
        deploy.mkdir()
        (deploy / "watchdog.sh").write_text("#!/bin/bash\n", newline="\n")
        self.installer = deploy / "install-watchdog.sh"
        # Isolate the copied installer's unit destination. The real /etc and systemd are never used.
        self.installer.write_text((ROOT / "deploy/install-watchdog.sh").read_text().replace(
            "/etc/systemd/system", str(self.unit_dir)), newline="\n")
        self.log = self.root / "systemctl-calls"
        commands = {
            "id": "#!/bin/bash\necho 0\n",
            "swapon": "#!/bin/bash\necho fake-swap\n",
            "systemctl": r'''#!/bin/bash
printf '%s\n' "$*" >> "$FAKE_SYSTEMCTL_LOG"
if [ "$*" = "${FAKE_SYSTEMCTL_FAIL:-}" ]; then
  echo "systemctl failure: $*" >&2
  exit 1
fi
''',
        }
        for name, content in commands.items():
            path = self.root / name
            path.write_text(content, newline="\n")
            path.chmod(0o700)
        self.env = {**os.environ, "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
                    "FAKE_SYSTEMCTL_LOG": str(self.log)}

    def run_installer(self, **values):
        return subprocess.run(["bash", str(self.installer)], env={**self.env, **values},
                              capture_output=True, text=True, timeout=30)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_failed_docker_boot_enable_is_reported_before_timer_install(self):
        result = self.run_installer(FAKE_SYSTEMCTL_FAIL="enable docker")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("systemctl failure: enable docker", result.stderr)
        self.assertIn("Could not enable Docker at boot", result.stderr)
        self.assertNotIn("Watchdog installed.", result.stdout)
        self.assertEqual(self.calls(), ["enable docker"])
        self.assertEqual(list(self.unit_dir.iterdir()), [])

    def test_failed_timer_enable_never_reports_success(self):
        result = self.run_installer(FAKE_SYSTEMCTL_FAIL="enable --now aidocumines-watchdog.timer")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("systemctl failure: enable --now aidocumines-watchdog.timer", result.stderr)
        self.assertNotIn("Watchdog installed.", result.stdout)

    def test_success_enables_docker_and_timer_with_isolated_units(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ["enable docker", "daemon-reload",
                         "enable --now aidocumines-watchdog.timer", "list-timers aidocumines-watchdog.timer --no-pager"])
        self.assertTrue((self.unit_dir / "aidocumines-watchdog.service").exists())
        self.assertTrue((self.unit_dir / "aidocumines-watchdog.timer").exists())
        self.assertIn("Watchdog installed.", result.stdout)


if __name__ == "__main__": unittest.main(verbosity=2)
