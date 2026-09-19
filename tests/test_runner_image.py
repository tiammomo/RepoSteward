from __future__ import annotations

import unittest
from pathlib import Path

RUNNER_DOCKERFILE = Path(__file__).resolve().parents[1] / "docker" / "Dockerfile.runner"


class RunnerImageContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dockerfile = RUNNER_DOCKERFILE.read_text(encoding="utf-8")

    def test_modelport_toolchains_are_pinned(self) -> None:
        self.assertIn(
            "FROM ghcr.io/astral-sh/uv:0.11.32@sha256:"
            "df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c AS uv",
            self.dockerfile,
        )
        self.assertIn(
            "FROM node:24.16.0-bookworm-slim@sha256:"
            "2c87ef9bd3c6a3bd4b472b4bec2ce9d16354b0c574f736c476489d09f560a203 AS node",
            self.dockerfile,
        )
        self.assertIn(
            "FROM rust:1.96.0-bookworm@sha256:"
            "5e2214abe154fe26e39f64488952e5c991eeed1d6d6da7cc8381ae83927f0cfc AS rust",
            self.dockerfile,
        )
        self.assertIn(
            "FROM python:3.12-bookworm@sha256:"
            "80f5d259a5969c86f6c92145d572de4a68c68e0edd28d4367dec0fb411b42af3",
            self.dockerfile,
        )
        self.assertIn("rustup component add clippy rustfmt", self.dockerfile)
        self.assertIn("RUSTUP_MAX_RETRIES=5 timeout 900", self.dockerfile)
        self.assertIn("for attempt in 1 2 3", self.dockerfile)
        self.assertIn("shellcheck=0.9.0-1", self.dockerfile)
        self.assertIn(
            "sed -i 's|http://deb.debian.org|https://deb.debian.org|g'",
            self.dockerfile,
        )
        self.assertIn(
            "apt-get -o Acquire::Retries=5 -o Acquire::https::Timeout=60 update",
            self.dockerfile,
        )
        self.assertIn(
            'ARG DOWNLOAD_CURL_FLAGS="--fail --location --silent --show-error '
            "--retry 5 --retry-all-errors --retry-max-time 1800 "
            '--connect-timeout 30 --max-time 900"',
            self.dockerfile,
        )
        self.assertIn(
            "RUN --mount=type=cache,id=reposteward-runner-downloads,"
            "target=/downloads,sharing=locked",
            self.dockerfile,
        )
        self.assertIn("curl $DOWNLOAD_CURL_FLAGS --continue-at -", self.dockerfile)
        for artifact in (
            "temurin.tar.gz",
            "maven.tar.gz",
            "protoc.zip",
            "helm.tar.gz",
        ):
            with self.subTest(artifact=artifact):
                self.assertIn(f"/downloads/{artifact}", self.dockerfile)
        self.assertIn('mv "$output_path.part" "$output_path"', self.dockerfile)

    def test_rust_toolchain_is_available_but_not_writable(self) -> None:
        self.assertIn(
            "COPY --from=rust /usr/local/cargo /usr/local/cargo", self.dockerfile
        )
        self.assertIn(
            "COPY --from=rust /usr/local/rustup /usr/local/rustup", self.dockerfile
        )
        self.assertIn(
            "chmod -R a+rX,a-w /usr/local/cargo /usr/local/rustup",
            self.dockerfile,
        )
        self.assertIn("RUSTUP_HOME=/usr/local/rustup", self.dockerfile)
        self.assertIn("PATH=/usr/local/cargo/bin:", self.dockerfile)
        self.assertIn("> /etc/profile.d/reposteward-toolchains.sh", self.dockerfile)
        self.assertIn(
            'export PATH="/usr/local/cargo/bin:/opt/protobuf/current/bin:',
            self.dockerfile,
        )
        self.assertNotIn("CARGO_HOME=/usr/local/cargo", self.dockerfile)

    def test_build_checks_tools_and_preserves_non_root_default(self) -> None:
        for command in (
            "python --version | grep -Fx 'Python 3.12.14'",
            "node --version | grep -Fx 'v24.16.0'",
            "pnpm --version | grep -Fx '10.33.2'",
            "rustc --version | grep -E '^rustc 1\\.96\\.0 '",
            "cargo --version | grep -E '^cargo 1\\.96\\.0 '",
            "cargo clippy --version",
            "cargo fmt --version",
            "shellcheck --version | grep -Fx 'version: 0.9.0'",
            "java -version 2>&1 | grep -F 'openjdk version",
            "mvn --version | grep -F 'Apache Maven 3.9.16'",
            "protoc --version | grep -Fx 'libprotoc 3.6.1'",
            "helm version --short | grep -E '^v3\\.21\\.0([+].*)?$'",
        ):
            with self.subTest(command=command):
                self.assertIn(command, self.dockerfile)
        self.assertIn(r"test \"\$(id -u)\" = \"1000\"", self.dockerfile)
        self.assertIn(r"test \"\$(id -g)\" = \"1000\"", self.dockerfile)
        self.assertIn('RUN bash -lc "set -eux;', self.dockerfile)
        self.assertIn("USER reposteward:reposteward", self.dockerfile)


if __name__ == "__main__":
    unittest.main()
