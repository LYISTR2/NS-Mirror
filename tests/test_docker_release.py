import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class DockerReleaseTests(unittest.TestCase):
    def read(self, name: str) -> str:
        return (ROOT / name).read_text(encoding="utf-8")

    def test_runtime_uses_configurable_persistent_data_directory(self):
        source = self.read("nodeseek_watcher.py")
        self.assertIn('DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)', source)
        self.assertIn('STATE_FILE = os.path.join(DATA_DIR, "state.json")', source)
        self.assertIn('KW_FILE = os.path.join(DATA_DIR, "keywords.json")', source)

    def test_dockerfile_runs_as_non_root_without_exposing_ports(self):
        dockerfile = self.read("Dockerfile")
        self.assertIn("USER 10001:10001", dockerfile)
        self.assertIn('ENV DATA_DIR="/data"', dockerfile)
        self.assertIn('CMD ["python", "/app/nodeseek_watcher.py"]', dockerfile)
        self.assertNotIn("EXPOSE", dockerfile)

    def test_compose_persists_data_and_hardens_container(self):
        compose = self.read("compose.yaml")
        self.assertIn("./data:/data", compose)
        self.assertIn("TG_BOT_TOKEN: ${TG_BOT_TOKEN", compose)
        self.assertIn("TG_CHAT_ID: ${TG_CHAT_ID", compose)
        self.assertIn("read_only: true", compose)
        self.assertIn("no-new-privileges:true", compose)
        self.assertIn("cap_drop:", compose)
        self.assertNotIn("ports:", compose)

    def test_one_click_installer_collects_credentials_before_docker_install(self):
        installer = self.read("docker-install.sh")
        self.assertIn('INSTALL_DIR="${INSTALL_DIR:-/opt/docker/ns-mirror}"', installer)
        self.assertIn("collect_credentials", installer)
        self.assertIn("install_docker", installer)
        self.assertLess(installer.rindex("collect_credentials\n"), installer.rindex("install_docker\n"))
        self.assertIn("TG_BOT_TOKEN", installer)
        self.assertIn("TG_CHAT_ID", installer)
        self.assertIn("--reconfigure", installer)

    def test_manager_supports_portable_backup_restore_and_update(self):
        manager = self.read("docker-manage.sh")
        for command in ("status", "logs", "restart", "update", "backup", "restore", "uninstall"):
            self.assertIn(command, manager)
        self.assertIn("docker compose", manager)
        self.assertIn("sha256sum", manager)

    def test_docker_runtime_files_are_excluded_from_git_and_build_context(self):
        gitignore = self.read(".gitignore")
        dockerignore = self.read(".dockerignore")
        self.assertIn(".env", gitignore)
        self.assertIn("data/", gitignore)
        self.assertIn(".env", dockerignore)
        self.assertIn("data/", dockerignore)


if __name__ == "__main__":
    unittest.main()
