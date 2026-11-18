"""Metadata readiness and storage contracts; all process calls use local doubles."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


class MetadataReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.executed = self.root / "executed"
        self.calls = self.root / "calls"
        (self.root / "curl").write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$CURL_LOG"
case "$*" in *unhealthy*) exit 22 ;; esac
n=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$COUNT_FILE"
[ "$n" -gt "${FAIL_CHECKS:-0}" ]
''')
        (self.root / "launch").write_text('''#!/bin/sh
printf '%s\\n' "$@" > "$EXEC_LOG"
''')
        for name in ("curl", "launch"):
            (self.root / name).chmod(0o700)
        self.env = {**os.environ, "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
                    "ETCD_USE_EMBED": "false", "ETCD_ENDPOINTS": "milvus_etcd:2379",
                    "MILVUS_ETCD_WAIT_TIMEOUT": "4", "MILVUS_ETCD_WAIT_INTERVAL": "1",
                    "CURL_LOG": str(self.calls), "COUNT_FILE": str(self.root / "count"),
                    "EXEC_LOG": str(self.executed)}

    def run_gate(self, *, command=True, **env):
        args = ["/bin/sh", str(ROOT / "deploy/milvus/wait-for-etcd.sh")]
        if command:
            args += [str(self.root / "launch"), "literal", "argument with spaces"]
        return subprocess.run(args, env={**self.env, **env}, capture_output=True, text=True, timeout=12)

    def test_ready_metadata_executes_original_command_with_arguments_preserved(self):
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.executed.read_text().splitlines(), ["literal", "argument with spaces"])
        self.assertIn("metadata is ready", result.stdout)
        self.assertIn("--noproxy *", self.calls.read_text())
        self.assertIn("http://milvus_etcd:2379/health", self.calls.read_text())

    def test_transient_metadata_failure_defers_milvus_instead_of_starting_early(self):
        result = self.run_gate(FAIL_CHECKS="2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.calls.read_text().splitlines()), 3)
        self.assertTrue(self.executed.exists())

    def test_metadata_timeout_fails_closed_without_launching_milvus(self):
        result = self.run_gate(ETCD_ENDPOINTS="unhealthy:2379", MILVUS_ETCD_WAIT_TIMEOUT="1")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.executed.exists())
        self.assertIn("refusing to start Milvus", result.stderr)

    def test_multiple_endpoints_are_checked_without_shell_evaluation(self):
        result = self.run_gate(ETCD_ENDPOINTS="http://unhealthy:2379,http://milvus_etcd:2379")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)

    def test_embedded_or_missing_external_configuration_is_rejected(self):
        for env in ({"ETCD_USE_EMBED": "true"}, {"ETCD_USE_EMBED": ""}, {"ETCD_ENDPOINTS": ""}):
            with self.subTest(env=env):
                self.assertEqual(self.run_gate(**env).returncode, 2)
        self.assertFalse(self.executed.exists())
        self.assertFalse(self.calls.exists())

    def test_invalid_wait_settings_and_missing_command_are_rejected(self):
        for env in ({"MILVUS_ETCD_WAIT_TIMEOUT": "0"}, {"MILVUS_ETCD_WAIT_TIMEOUT": "bad"},
                    {"MILVUS_ETCD_WAIT_INTERVAL": "0"}, {"MILVUS_ETCD_WAIT_INTERVAL": "-1"}):
            with self.subTest(env=env):
                self.assertEqual(self.run_gate(**env).returncode, 2)
        self.assertEqual(self.run_gate(command=False).returncode, 2)
        self.assertFalse(self.executed.exists())


class MetadataComposeContracts(unittest.TestCase):
    def configs(self):
        for path, storage in ((ROOT / "docker-compose.yml", "milvus-data"),
                              (ROOT / "deploy/compose.audit.yml", "audit_milvus")):
            yield path, storage, yaml.safe_load(path.read_text(encoding="utf-8"))

    def test_same_metadata_version_member_and_storage_paths_are_preserved(self):
        for path, storage, config in self.configs():
            with self.subTest(compose=path.name):
                etcd = config["services"]["milvus_etcd"]
                milvus = config["services"]["milvus"]
                self.assertEqual(etcd["image"], "quay.io/coreos/etcd:v3.5.23")
                self.assertEqual(milvus["image"], "milvusdb/milvus:v2.6.17")
                for item in ("--name=default", "--data-dir=/var/lib/milvus/etcd",
                             "--listen-peer-urls=http://localhost:2380",
                             "--initial-advertise-peer-urls=http://localhost:2380",
                             "--initial-cluster=default=http://localhost:2380"):
                    self.assertIn(item, etcd["command"])
                for service in (milvus, etcd):
                    self.assertIn(storage + ":/var/lib/milvus", service["volumes"])
                self.assertEqual(milvus["environment"]["COMMON_STORAGETYPE"], "local")

    def test_metadata_has_no_host_ports_or_shared_application_network(self):
        for path, _, config in self.configs():
            with self.subTest(compose=path.name):
                etcd = config["services"]["milvus_etcd"]
                self.assertFalse(etcd.get("ports"))
                self.assertEqual(etcd["networks"], ["milvus-metadata"])
                self.assertIs(config["networks"]["milvus-metadata"]["internal"], True)
                joined = {name for name, service in config["services"].items()
                          if "milvus-metadata" in service.get("networks", [])}
                self.assertEqual(joined, {"milvus", "milvus_etcd"})

    def test_health_gate_runs_at_compose_and_each_container_start_preserving_tini(self):
        for path, _, config in self.configs():
            with self.subTest(compose=path.name):
                milvus = config["services"]["milvus"]
                self.assertEqual(milvus["entrypoint"], ["/tini", "--", "/bin/sh", "/milvus/wait-for-etcd.sh"])
                self.assertEqual(milvus["command"], ["milvus", "run", "standalone"])
                self.assertEqual(milvus["depends_on"]["milvus_etcd"]["condition"], "service_healthy")
                self.assertEqual(milvus["environment"]["ETCD_USE_EMBED"], "false")
                self.assertEqual(milvus["environment"]["ETCD_ENDPOINTS"], "milvus_etcd:2379")
                self.assertFalse(milvus.get("security_opt"))
                self.assertTrue(any("wait-for-etcd.sh:/milvus/wait-for-etcd.sh:ro" in value
                                    for value in milvus["volumes"]))

    def test_etcd_health_uses_real_proposal_without_requiring_a_shell(self):
        for path, _, config in self.configs():
            with self.subTest(compose=path.name):
                probe = config["services"]["milvus_etcd"]["healthcheck"]["test"]
                self.assertEqual(probe[:2], ["CMD", "etcdctl"])
                self.assertIn("--endpoints=http://127.0.0.1:2379", probe)
                self.assertEqual(probe[-2:], ["endpoint", "health"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
