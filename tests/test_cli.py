import json
import hashlib
import subprocess
from pathlib import Path
from zipfile import ZipFile

import pytest

from dbpm import cli
from dbpm.db import ApplicationState, DeploymentMetadata, OperationRecord, SqlResult
from dbpm.registry import RegistryResolution, RegistrySource


def _write_package(path: Path) -> None:
    path.mkdir()
    (path / "dbpm.yaml").write_text(
        """
package:
  name: demo
  version: "0.1.0"

scripts:
  install: Deployment_Manifests/deploy.sql
  validate: Tests/smoke_test.sql
""",
        encoding="utf-8",
    )


def _write_workspace_package(
    path: Path,
    name: str,
    version: str = "0.1.0",
    *,
    dependencies: str = "",
    publish: str = "",
) -> None:
    path.mkdir(parents=True)
    (path / "dbpm.yaml").write_text(
        f"""
package:
  name: {name}
  version: "{version}"

{dependencies}
{publish}
scripts:
  install: deploy.sql
  validate: validate.sql
""",
        encoding="utf-8",
    )
    (path / "deploy.sql").write_text("PROMPT deploy\n", encoding="utf-8")
    (path / "validate.sql").write_text("PROMPT validate\n", encoding="utf-8")


def _write_workspace_manifest(path: Path, package_paths: list[str]) -> None:
    entries = "\n".join(f"    - {item}" for item in package_paths)
    (path / "dbpm-workspace.yaml").write_text(
        f"""
workspace:
  packages:
{entries}
""",
        encoding="utf-8",
    )


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _write_registry_zip(
    path: Path,
    name: str,
    version: str,
    *,
    dependencies: str = "",
) -> None:
    with ZipFile(path, "w") as archive:
        archive.writestr(
            f"{name}/dbpm.yaml",
            f"""
package:
  name: {name}
  version: "{version}"

{dependencies}
scripts:
  install: deploy.sql
""",
        )
        archive.writestr(f"{name}/deploy.sql", "PROMPT deploy\n")


def _registry_resolution(
    package: str,
    version: str,
    artifact_url: str,
    checksum: str,
    constraint: str,
    *,
    warning: dict[str, object] | None = None,
) -> RegistryResolution:
    warnings = [warning] if warning else []
    return RegistryResolution(
        package=package,
        version=version,
        artifact_url=artifact_url,
        artifact_checksum=checksum,
        artifact_signature_url=f"{artifact_url}.asc",
        publisher_key_fingerprint="FINGERPRINT",
        registry_url="https://registry.example",
        source=RegistrySource(package, constraint),
        warning=warning,
        warnings=warnings,
    )


def test_generate_scripts_without_from_generates_initial_install_only(tmp_path: Path, capsys):
    repo = tmp_path / "script_repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "dbpm.yaml").write_text(
        """
package:
  name: demo
  version: "0.1.0"

scripts:
  install: sql/install.sql
  upgrade: sql/update.sql
generation:
  release_upgrade_output: sql/releases/0.1.0/update.sql
""".lstrip(),
        encoding="utf-8",
    )
    (repo / "Tables").mkdir()
    (repo / "Tables" / "DEMO.sql").write_text("CREATE TABLE DEMO (ID NUMBER);\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")

    assert cli.main(["generate-scripts", str(repo)]) == 0

    assert capsys.readouterr().out.strip() == "WROTE=sql/install.sql"
    assert (repo / "sql" / "install.sql").exists()
    assert not (repo / "sql" / "update.sql").exists()
    assert not (repo / "sql" / "releases" / "0.1.0" / "update.sql").exists()


def test_registry_help_prefers_machine_hostname(capsys):
    parser = cli._build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["plan", "--help"])

    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "https://registry.dbpm.io" in help_text
    assert "https://dbpm.io" not in help_text


@pytest.fixture(autouse=True)
def _no_reverse_dependencies(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DBPM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("DBPM_CONNECT", raising=False)
    monkeypatch.delenv("DBPM_CONNECT_NAME", raising=False)
    monkeypatch.delenv("DBPM_DB_USER", raising=False)
    monkeypatch.delenv("DBPM_DB_PASSWORD", raising=False)
    monkeypatch.delenv("DBPM_DB_DSN", raising=False)
    monkeypatch.delenv("DBPM_SQL_RUNNER", raising=False)
    monkeypatch.setattr(cli, "get_reverse_dependencies", lambda **kwargs: [])
    monkeypatch.setattr(
        cli,
        "get_core_deployment_metadata",
        lambda **kwargs: DeploymentMetadata(deploy_locked="N", deploy_environment="DEV"),
    )
    monkeypatch.setattr(
        cli,
        "get_target_identity",
        lambda **kwargs: cli.TargetIdentity(service_name="db", schema_name="APP"),
    )


def test_plan_prints_json(tmp_path: Path, capsys):
    package = tmp_path / "package"
    _write_package(package)

    assert cli.main(["plan", str(package)]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["schema_version"] == "dbpm.plan.v0"
    assert output["package"]["application_name"] == "DEMO"
    assert output["policy"]["policy_context"] == {
        "deployment_locked": False,
        "source": "default",
    }


def test_plan_accepts_disconnected_locked_policy(tmp_path: Path, capsys):
    package = tmp_path / "package"
    _write_package(package)

    assert cli.main(["plan", str(package), "--policy", "locked"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["policy"]["policy_context"] == {
        "deployment_locked": True,
        "source": "cli-policy",
    }


def test_plan_bootstrap_core_accepts_deploy_environment(tmp_path: Path, capsys):
    package = tmp_path / "core"
    _write_core_bootstrap_package(package)

    assert (
        cli.main(
            [
                "plan",
                str(package),
                "--mode",
                "bootstrap-core",
                "--policy",
                "locked",
                "--deploy-environment",
                "PROD",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["policy"]["policy_context"] == {
        "deployment_locked": True,
        "source": "cli-policy",
        "deploy_environment": "PROD",
    }
    assert output["execution"]["stdin"] == "Y\nPROD\n"


def test_deploy_environment_rejected_for_non_bootstrap_plan(tmp_path: Path, capsys):
    package = tmp_path / "package"
    _write_package(package)

    assert cli.main(["plan", str(package), "--deploy-environment", "PROD"]) == 2

    assert "--deploy-environment is only supported for bootstrap-core" in capsys.readouterr().err


def test_connected_plan_reads_core_deploy_locked(tmp_path: Path, monkeypatch, capsys):
    package = tmp_path / "package"
    _write_package(package)
    monkeypatch.setattr(
        cli,
        "get_core_deployment_metadata",
        lambda **kwargs: DeploymentMetadata(deploy_locked="Y", deploy_environment="PLAB"),
    )
    monkeypatch.setattr(cli, "get_application_state", lambda **kwargs: None)

    assert cli.main(["plan", str(package), "--connect", "user/pass@db"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["policy"]["policy_context"] == {
        "deployment_locked": True,
        "source": "core-dictionary",
        "deploy_environment": "PLAB",
    }


def test_connected_plan_rejects_cli_policy_override(tmp_path: Path, capsys):
    package = tmp_path / "package"
    _write_package(package)

    assert cli.main(["plan", str(package), "--connect", "user/pass@db", "--policy", "locked"]) == 2

    assert "--policy is only supported without database access" in capsys.readouterr().err


def test_env_flag_is_rejected(tmp_path: Path, capsys):
    package = tmp_path / "package"
    _write_package(package)

    with pytest.raises(SystemExit) as exc:
        cli.main(["plan", str(package), "--env", "development"])

    assert exc.value.code == 2
    assert "unrecognized arguments: --env development" in capsys.readouterr().err


def test_plan_with_dependency_source_prints_multi_package_plan(tmp_path: Path, capsys):
    base = tmp_path / "base"
    consumer = tmp_path / "consumer"
    _write_package(base)
    consumer.mkdir()
    (consumer / "dbpm.yaml").write_text(
        """
package:
  name: consumer
  version: "0.1.0"

dependencies:
  - name: demo
    version: "0.1.0"

scripts:
  install: deploy.sql
""",
        encoding="utf-8",
    )

    assert (
        cli.main(
            [
                "plan",
                str(consumer),
                "--dependency-source",
                str(base),
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["schema_version"] == "dbpm.multi-plan.v0"
    assert output["execution_order"] == ["DEMO", "CONSUMER"]


def test_plan_with_missing_dependency_source_fails(tmp_path: Path, capsys):
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    (consumer / "dbpm.yaml").write_text(
        """
package:
  name: consumer
  version: "0.1.0"

dependencies:
  - name: demo
    version: "0.1.0"

scripts:
  install: deploy.sql
""",
        encoding="utf-8",
    )

    assert cli.main(["plan", str(consumer)]) == 2

    assert "Missing dependency source for CONSUMER: DEMO 0.1.0" in capsys.readouterr().err


def test_workspace_list_prints_package_summaries(tmp_path: Path, capsys):
    _write_workspace_package(tmp_path / "database" / "utl_interval", "utl_interval", "1.0.0")
    _write_workspace_package(tmp_path / "database" / "simple_scheduler", "simple_scheduler", "1.1.0")
    _write_workspace_manifest(
        tmp_path,
        ["database/utl_interval", "database/simple_scheduler"],
    )

    assert cli.main(["workspace", "list", str(tmp_path)]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["workspace_root"] == str(tmp_path)
    assert [package["name"] for package in output["packages"]] == [
        "utl_interval",
        "simple_scheduler",
    ]


def test_plan_workspace_root_selects_package(tmp_path: Path, capsys):
    _write_workspace_package(tmp_path / "database" / "utl_interval", "utl_interval", "1.0.0")
    _write_workspace_package(tmp_path / "database" / "simple_scheduler", "simple_scheduler", "1.1.0")
    _write_workspace_manifest(
        tmp_path,
        ["database/utl_interval", "database/simple_scheduler"],
    )

    assert cli.main(["plan", str(tmp_path), "--package", "simple_scheduler"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["schema_version"] == "dbpm.plan.v0"
    assert output["package"]["application_name"] == "SIMPLE_SCHEDULER"
    assert output["source"]["path"].endswith("database/simple_scheduler")


def test_plan_workspace_root_without_package_fails_when_ambiguous(tmp_path: Path, capsys):
    _write_workspace_package(tmp_path / "database" / "utl_interval", "utl_interval", "1.0.0")
    _write_workspace_package(tmp_path / "database" / "simple_scheduler", "simple_scheduler", "1.1.0")
    _write_workspace_manifest(
        tmp_path,
        ["database/utl_interval", "database/simple_scheduler"],
    )

    assert cli.main(["plan", str(tmp_path)]) == 2

    assert "Workspace contains multiple packages" in capsys.readouterr().err


def test_plan_workspace_root_auto_uses_sibling_dependency(tmp_path: Path, capsys):
    _write_workspace_package(tmp_path / "database" / "utl_interval", "utl_interval", "1.0.0")
    _write_workspace_package(
        tmp_path / "database" / "simple_scheduler",
        "simple_scheduler",
        "1.1.0",
        dependencies="""
dependencies:
  - name: utl_interval
    version: "1.0.0"
""",
    )
    _write_workspace_manifest(
        tmp_path,
        ["database/utl_interval", "database/simple_scheduler"],
    )

    assert cli.main(["plan", str(tmp_path), "--package", "simple_scheduler"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["schema_version"] == "dbpm.multi-plan.v0"
    assert output["execution_order"] == ["UTL_INTERVAL", "SIMPLE_SCHEDULER"]


def test_explicit_dependency_source_overrides_workspace_sibling(tmp_path: Path, capsys):
    explicit = tmp_path / "explicit_interval"
    _write_workspace_package(tmp_path / "database" / "utl_interval", "utl_interval", "1.0.0")
    _write_workspace_package(
        tmp_path / "database" / "simple_scheduler",
        "simple_scheduler",
        "1.1.0",
        dependencies="""
dependencies:
  - name: utl_interval
    version: "2.0.0"
""",
    )
    _write_workspace_package(explicit, "utl_interval", "2.0.0")
    _write_workspace_manifest(
        tmp_path,
        ["database/utl_interval", "database/simple_scheduler"],
    )

    assert (
        cli.main(
            [
                "plan",
                str(tmp_path),
                "--package",
                "simple_scheduler",
                "--dependency-source",
                str(explicit),
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["execution_order"] == ["UTL_INTERVAL", "SIMPLE_SCHEDULER"]
    dep_plan = output["packages"][0]
    assert dep_plan["package"]["version"] == "2.0.0"


def test_publish_workspace_root_dry_run_selects_package(tmp_path: Path, capsys):
    _write_workspace_package(
        tmp_path / "database" / "utl_interval",
        "utl_interval",
        "1.0.0",
        publish="""
publish:
  group: com.example.database
""",
    )
    _write_workspace_package(tmp_path / "database" / "simple_scheduler", "simple_scheduler", "1.1.0")
    _write_workspace_manifest(
        tmp_path,
        ["database/utl_interval", "database/simple_scheduler"],
    )

    assert (
        cli.main(
            [
                "publish",
                str(tmp_path),
                "--package",
                "utl_interval",
                "--target",
                "maven:https://repo.example.test/releases",
                "--signing-key",
                "signing@example.test",
                "--dry-run",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "DRY_RUN: would publish utl_interval-1.0.0.zip" in output
    assert "group:     com.example.database" in output


def test_plan_registry_source_auto_resolves_dependencies(tmp_path: Path, monkeypatch, capsys):
    scheduler = tmp_path / "simple_scheduler.zip"
    interval = tmp_path / "utl_interval.zip"
    _write_registry_zip(
        scheduler,
        "simple_scheduler",
        "1.1.0",
        dependencies="""
dependencies:
  - name: utl_interval
    version: "^1.0.0"
""",
    )
    _write_registry_zip(interval, "utl_interval", "1.0.0")
    artifacts = {
        "https://repo.example/simple_scheduler-1.1.0.zip": scheduler,
        "https://repo.example/utl_interval-1.0.0.zip": interval,
    }
    checksums = {
        url: hashlib.sha256(path.read_bytes()).hexdigest()
        for url, path in artifacts.items()
    }
    resolved = {
        "registry:simple_scheduler@^1.1.0": _registry_resolution(
            "simple_scheduler",
            "1.1.0",
            "https://repo.example/simple_scheduler-1.1.0.zip",
            checksums["https://repo.example/simple_scheduler-1.1.0.zip"],
            "^1.1.0",
            warning={"code": "yanked_version", "message": "Yanked"},
        ),
        "registry:utl_interval@^1.0.0": _registry_resolution(
            "utl_interval",
            "1.0.0",
            "https://repo.example/utl_interval-1.0.0.zip",
            checksums["https://repo.example/utl_interval-1.0.0.zip"],
            "^1.0.0",
        ),
    }

    monkeypatch.setattr("dbpm.source.resolve_registry_source", lambda raw, registry_url=None: resolved[raw])
    monkeypatch.setattr("dbpm.source._check_gpg_signature", lambda *args: None)

    def fake_download(url: str, destination: Path) -> None:
        if url.endswith(".asc"):
            destination.write_bytes(b"sig")
        else:
            destination.write_bytes(artifacts[url].read_bytes())

    monkeypatch.setattr("dbpm.source._download", fake_download)

    assert (
        cli.main(
            [
                "plan",
                "registry:simple_scheduler@^1.1.0",
                "--registry-url",
                "https://registry.example",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["schema_version"] == "dbpm.multi-plan.v0"
    assert output["execution_order"] == ["UTL_INTERVAL", "SIMPLE_SCHEDULER"]
    assert output["packages"][1]["warnings"] == [{"code": "yanked_version", "message": "Yanked"}]


def test_explicit_dependency_source_wins_over_registry_auto_resolution(tmp_path: Path, monkeypatch, capsys):
    scheduler = tmp_path / "simple_scheduler.zip"
    explicit = tmp_path / "explicit_interval"
    _write_registry_zip(
        scheduler,
        "simple_scheduler",
        "1.1.0",
        dependencies="""
dependencies:
  - name: utl_interval
    version: "1.0.0"
""",
    )
    explicit.mkdir()
    (explicit / "dbpm.yaml").write_text(
        """
package:
  name: utl_interval
  version: "1.0.0"

scripts:
  install: deploy.sql
""",
        encoding="utf-8",
    )
    (explicit / "deploy.sql").write_text("PROMPT deploy\n", encoding="utf-8")
    checksum = hashlib.sha256(scheduler.read_bytes()).hexdigest()

    def fake_resolve(raw: str, registry_url: str | None = None) -> RegistryResolution:
        if raw != "registry:simple_scheduler@^1.1.0":
            pytest.fail(f"unexpected registry dependency lookup: {raw}")
        return _registry_resolution(
            "simple_scheduler",
            "1.1.0",
            "https://repo.example/simple_scheduler-1.1.0.zip",
            checksum,
            "^1.1.0",
        )

    monkeypatch.setattr("dbpm.source.resolve_registry_source", fake_resolve)
    monkeypatch.setattr("dbpm.source._check_gpg_signature", lambda *args: None)
    monkeypatch.setattr(
        "dbpm.source._download",
        lambda url, destination: destination.write_bytes(b"sig" if url.endswith(".asc") else scheduler.read_bytes()),
    )

    assert (
        cli.main(
            [
                "plan",
                "registry:simple_scheduler@^1.1.0",
                "--dependency-source",
                str(explicit),
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["execution_order"] == ["UTL_INTERVAL", "SIMPLE_SCHEDULER"]


def test_install_from_registry_lockfile_does_not_call_registry(tmp_path: Path, monkeypatch, capsys):
    package = tmp_path / "utl_interval.zip"
    lockfile = tmp_path / "dbpm-lock.json"
    _write_registry_zip(package, "utl_interval", "1.0.0")
    artifact_url = "https://repo.example/utl_interval-1.0.0.zip"
    checksum = hashlib.sha256(package.read_bytes()).hexdigest()

    monkeypatch.setattr(
        "dbpm.source.resolve_registry_source",
        lambda raw, registry_url=None: _registry_resolution(
            "utl_interval",
            "1.0.0",
            artifact_url,
            checksum,
            "1.0.0",
        ),
    )
    monkeypatch.setattr("dbpm.source._check_gpg_signature", lambda *args: None)

    def fake_download(url: str, destination: Path) -> None:
        destination.write_bytes(b"sig" if url.endswith(".asc") else package.read_bytes())

    monkeypatch.setattr("dbpm.source._download", fake_download)

    assert cli.main(["lock", "registry:utl_interval@1.0.0", "--output", str(lockfile)]) == 0
    locked = json.loads(lockfile.read_text(encoding="utf-8"))["packages"][0]
    assert locked["artifact"]["uri"] == artifact_url
    assert locked["artifact"]["signature_url"] == f"{artifact_url}.asc"
    assert locked["artifact"]["publisher_key_fingerprint"] == "FINGERPRINT"
    capsys.readouterr()

    def fail_registry_lookup(raw: str, registry_url: str | None = None) -> RegistryResolution:
        pytest.fail("locked install should not call registry")

    monkeypatch.setattr("dbpm.source.resolve_registry_source", fail_registry_lookup)

    assert cli.main(["install", "--lockfile", str(lockfile), "--dry-run"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["package"]["application_name"] == "UTL_INTERVAL"


def test_lock_writes_lockfile(tmp_path: Path, capsys):
    package = tmp_path / "package"
    lockfile = tmp_path / "dbpm-lock.json"
    _write_package(package)

    assert cli.main(["lock", str(package), "--output", str(lockfile)]) == 0

    output = json.loads(lockfile.read_text(encoding="utf-8"))
    assert output["schema_version"] == "dbpm.lock.v0"
    assert output["execution_order"] == ["DEMO"]
    assert f"WROTE_LOCKFILE={lockfile}" in capsys.readouterr().out


def test_lock_check_rejects_mismatch(tmp_path: Path, capsys):
    package = tmp_path / "package"
    lockfile = tmp_path / "dbpm-lock.json"
    _write_package(package)

    assert cli.main(["lock", str(package), "--output", str(lockfile)]) == 0
    data = json.loads(lockfile.read_text(encoding="utf-8"))
    data["packages"][0]["version"] = "9.9.9"
    lockfile.write_text(json.dumps(data), encoding="utf-8")

    assert cli.main(["lock", str(package), "--output", str(lockfile), "--check"]) == 2

    assert "DEMO version mismatch" in capsys.readouterr().err


def test_lock_check_db_reconciles_installed_state(tmp_path: Path, monkeypatch, capsys):
    package = tmp_path / "package"
    lockfile = tmp_path / "dbpm-lock.json"
    _write_package(package)
    monkeypatch.setattr(
        cli,
        "get_application_state",
        lambda **kwargs: ApplicationState(
            application_name="DEMO",
            version="0.1.0",
            deploy_status="C",
            deploy_commit_hash="abc",
        ),
    )

    assert cli.main(["lock", str(package), "--output", str(lockfile)]) == 0
    locked = json.loads(lockfile.read_text(encoding="utf-8"))["packages"][0]
    monkeypatch.setattr(
        cli,
        "get_deployment_provenance",
        lambda **kwargs: {
            "major_version": 0,
            "minor_version": 1,
            "patch_version": 0,
            "artifact_uri": locked["artifact"]["uri"],
            "artifact_checksum": locked["artifact"]["checksum"],
            "artifact_checksum_alg": locked["artifact"]["checksum_alg"],
            "artifact_file_name": locked["artifact"]["file_name"],
            "artifact_repository_type": locked["artifact"]["repository_type"],
            "artifact_group_id": locked["artifact"]["group_id"],
            "artifact_id": locked["artifact"]["artifact_id"],
            "artifact_version": locked["artifact"]["artifact_version"],
            "artifact_classifier": locked["artifact"]["classifier"],
            "artifact_extension": locked["artifact"]["extension"],
            "package_coordinate": locked["artifact"]["coordinate"],
            "source_repository_url": locked["provenance"]["source_repository_url"],
            "source_commit_hash": locked["provenance"]["source_commit_hash"],
            "source_path": locked["artifact"]["uri"],
            "build_id": locked["provenance"]["build_id"],
            "build_url": locked["provenance"]["build_url"],
            "build_time": locked["provenance"]["build_time"],
        },
    )
    assert (
        cli.main(
            [
                "lock",
                str(package),
                "--output",
                str(lockfile),
                "--check",
                "--check-db",
                "--connect",
                "user/pass@db",
            ]
        )
        == 0
    )

    assert f"LOCKFILE_OK={lockfile}" in capsys.readouterr().out


def test_lock_check_db_keeps_dependency_sources_when_dependency_is_installed(
    tmp_path: Path,
    monkeypatch,
):
    base = tmp_path / "base"
    consumer = tmp_path / "consumer"
    lockfile = tmp_path / "dbpm-lock.json"
    _write_package(base)
    consumer.mkdir()
    (consumer / "dbpm.yaml").write_text(
        """
package:
  name: consumer
  version: "0.1.0"

dependencies:
  - name: demo
    version: "0.1.0"

scripts:
  install: deploy.sql
""",
        encoding="utf-8",
    )

    def fake_get_application_state(**kwargs):
        return ApplicationState(
            application_name=kwargs["application_name"],
            version="0.1.0",
            deploy_status="C",
            deploy_commit_hash="abc",
        )

    monkeypatch.setattr(cli, "get_application_state", fake_get_application_state)
    monkeypatch.setattr(cli, "get_deployment_provenance", lambda **kwargs: _matching_provenance_from_lock(lockfile, kwargs["application_name"]))

    assert (
        cli.main(
            [
                "lock",
                str(consumer),
                "--dependency-source",
                str(base),
                "--output",
                str(lockfile),
            ]
        )
        == 0
    )

    output = json.loads(lockfile.read_text(encoding="utf-8"))
    assert output["execution_order"] == ["DEMO", "CONSUMER"]
    assert (
        cli.main(
            [
                "lock",
                str(consumer),
                "--dependency-source",
                str(base),
                "--output",
                str(lockfile),
                "--check",
                "--check-db",
                "--connect",
                "user/pass@db",
            ]
        )
        == 0
    )


def _matching_provenance_from_lock(lockfile: Path, application_name: str) -> dict[str, object]:
    data = json.loads(lockfile.read_text(encoding="utf-8"))
    package = next(item for item in data["packages"] if item["application_name"] == application_name)
    major, minor, patch = package["version"].split(".")
    return {
        "major_version": int(major),
        "minor_version": int(minor),
        "patch_version": int(patch),
        "artifact_uri": package["artifact"]["uri"],
        "artifact_checksum": package["artifact"]["checksum"],
        "artifact_checksum_alg": package["artifact"]["checksum_alg"],
        "artifact_file_name": package["artifact"]["file_name"],
        "artifact_repository_type": package["artifact"]["repository_type"],
        "artifact_group_id": package["artifact"]["group_id"],
        "artifact_id": package["artifact"]["artifact_id"],
        "artifact_version": package["artifact"]["artifact_version"],
        "artifact_classifier": package["artifact"]["classifier"],
        "artifact_extension": package["artifact"]["extension"],
        "package_coordinate": package["artifact"]["coordinate"],
        "source_repository_url": package["provenance"]["source_repository_url"],
        "source_commit_hash": package["provenance"]["source_commit_hash"],
        "source_path": package["artifact"]["uri"],
        "build_id": package["provenance"]["build_id"],
        "build_url": package["provenance"]["build_url"],
        "build_time": package["provenance"]["build_time"],
    }


def test_lock_check_db_requires_check(tmp_path: Path, capsys):
    package = tmp_path / "package"
    _write_package(package)

    assert cli.main(["lock", str(package), "--check-db"]) == 2

    assert "--check-db requires --check" in capsys.readouterr().err


def test_install_with_dependency_source_executes_multi_package_plan(
    tmp_path: Path, monkeypatch, capsys
):
    base = tmp_path / "base"
    consumer = tmp_path / "consumer"
    _write_package(base)
    consumer.mkdir()
    (consumer / "dbpm.yaml").write_text(
        """
package:
  name: consumer
  version: "0.1.0"

dependencies:
  - name: demo
    version: "0.1.0"

scripts:
  install: deploy.sql
""",
        encoding="utf-8",
    )
    calls = {}

    monkeypatch.setattr(cli, "get_application_state", lambda **kwargs: None)

    def fake_execute_plan(plan, *, connect: str, runner: str, runtime_prefix: str | None = None):
        calls["plan"] = plan
        calls["connect"] = connect
        calls["runner"] = runner
        return 0

    monkeypatch.setattr(cli, "execute_plan", fake_execute_plan)

    assert (
        cli.main(
            [
                "install",
                str(consumer),
                "--dependency-source",
                str(base),
                "--connect",
                "user/pass@db",
            ]
        )
        == 0
    )

    assert calls["connect"] == "user/pass@db"
    assert calls["plan"]["schema_version"] == "dbpm.multi-plan.v0"
    assert calls["plan"]["execution_order"] == ["DEMO", "CONSUMER"]
    stderr = capsys.readouterr().err
    assert "dbpm: Preparing install plan..." in stderr
    assert "dbpm: Checking database and policy state for 2 packages..." in stderr
    assert "dbpm: Install completed successfully: consumer 0.1.0" in stderr
    assert "Loading root package source" not in stderr
    assert "Reading installed state" not in stderr


def test_install_from_lockfile_executes_locked_plan(tmp_path: Path, monkeypatch):
    package = tmp_path / "package"
    lockfile = tmp_path / "dbpm-lock.json"
    _write_package(package)
    calls = {}

    monkeypatch.setattr(cli, "get_application_state", lambda **kwargs: None)

    def fake_execute_plan(plan, *, connect: str, runner: str, runtime_prefix: str | None = None):
        calls["plan"] = plan
        calls["connect"] = connect
        calls["runner"] = runner
        return 0

    monkeypatch.setattr(cli, "execute_plan", fake_execute_plan)

    assert cli.main(["lock", str(package), "--output", str(lockfile)]) == 0
    assert cli.main(["install", "--lockfile", str(lockfile), "--connect", "user/pass@db"]) == 0

    assert calls["connect"] == "user/pass@db"
    assert calls["plan"]["package"]["application_name"] == "DEMO"


def test_install_from_lockfile_verifies_full_resolution_before_omitting_satisfied_dependency(
    tmp_path: Path,
    monkeypatch,
):
    dependency = tmp_path / "dependency"
    consumer = tmp_path / "consumer"
    lockfile = tmp_path / "dbpm-lock.json"
    _write_workspace_package(dependency, "dependency", version="1.0.0")
    _write_workspace_package(
        consumer,
        "consumer",
        dependencies=(
            "dependencies:\n"
            "  - name: dependency\n"
            '    version: "^1.0.0"\n'
        ),
    )

    assert (
        cli.main(
            [
                "lock",
                str(consumer),
                "--dependency-source",
                str(dependency),
                "--output",
                str(lockfile),
            ]
        )
        == 0
    )

    def application_state(*, application_name: str, **kwargs):
        if application_name == "DEPENDENCY":
            return ApplicationState(
                application_name="DEPENDENCY",
                version="1.0.0",
                deploy_status="C",
                deploy_commit_hash="a" * 40,
            )
        return None

    calls: dict[str, object] = {}
    monkeypatch.setattr(cli, "get_application_state", application_state)
    monkeypatch.setattr(cli, "get_reverse_dependencies", lambda **kwargs: [])
    monkeypatch.setattr(
        cli,
        "execute_plan",
        lambda plan, **kwargs: calls.setdefault("plan", plan),
    )

    assert (
        cli.main(
            [
                "install",
                "--lockfile",
                str(lockfile),
                "--connect",
                "user/pass@db",
            ]
        )
        == 0
    )

    plan = calls["plan"]
    assert plan["execution_order"] == ["CONSUMER"]
    assert [item["application_name"] for item in plan["satisfied_dependencies"]] == [
        "DEPENDENCY"
    ]


def test_install_from_default_lockfile_path(tmp_path: Path, monkeypatch):
    package = tmp_path / "package"
    _write_package(package)
    calls = {}

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "get_application_state", lambda **kwargs: None)
    monkeypatch.setattr(cli, "execute_plan", lambda plan, **kwargs: calls.setdefault("plan", plan))

    assert cli.main(["lock", str(package)]) == 0
    assert cli.main(["install", "--lockfile", "--connect", "user/pass@db"]) == 0

    assert calls["plan"]["package"]["application_name"] == "DEMO"


def test_install_from_lockfile_rejects_extra_sources(tmp_path: Path, capsys):
    package = tmp_path / "package"
    lockfile = tmp_path / "dbpm-lock.json"
    _write_package(package)

    assert cli.main(["lock", str(package), "--output", str(lockfile)]) == 0
    assert (
        cli.main(
            [
                "install",
                str(package),
                "--lockfile",
                str(lockfile),
                "--connect",
                "user/pass@db",
            ]
        )
        == 2
    )

    assert "--lockfile cannot be combined with source or --dependency-source" in capsys.readouterr().err


def test_install_dry_run_prints_plan(tmp_path: Path, capsys):
    package = tmp_path / "package"
    _write_package(package)

    assert cli.main(["install", str(package), "--dry-run"]) == 0

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output["mode"] == "install"
    assert "completed successfully" not in captured.err


def test_reinstall_dry_run_shows_required_destructive_flag(tmp_path: Path, capsys):
    package = tmp_path / "package"
    _write_package(package)

    assert cli.main(["reinstall", str(package), "--dry-run"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["policy"]["result"] == "requires-approval"
    assert "`reinstall` requires --allow-destructive" in output["policy"]["required_approvals"]


def test_install_without_connect_fails(tmp_path: Path, capsys):
    package = tmp_path / "package"
    _write_package(package)

    assert cli.main(["install", str(package)]) == 2

    err = capsys.readouterr().err
    assert "Database access requires --connect/DBPM_CONNECT" in err
    assert "DBPM_DB_USER/DBPM_DB_PASSWORD/DBPM_DB_DSN" in err
    assert "--connect-name/DBPM_CONNECT_NAME" in err


def test_install_blocks_when_package_already_installed(tmp_path: Path, monkeypatch, capsys):
    package = tmp_path / "package"
    _write_package(package)

    monkeypatch.setattr(
        cli,
        "get_application_state",
        lambda **kwargs: ApplicationState(
            application_name="DEMO",
            version="0.1.0",
            deploy_status="C",
            deploy_commit_hash="abc",
        ),
    )

    assert cli.main(["install", str(package), "--connect", "user/pass@db"]) == 2

    err = capsys.readouterr().err
    assert "DEMO is already installed; use reinstall or upgrade" in err
    assert f"dbpm upgrade {package}" in err
    assert f"dbpm reinstall {package} --allow-destructive" in err


def test_install_blocks_incomplete_existing_deployment(tmp_path: Path, monkeypatch, capsys):
    package = tmp_path / "package"
    _write_package(package)

    monkeypatch.setattr(
        cli,
        "get_application_state",
        lambda **kwargs: ApplicationState(
            application_name="DEMO",
            version="0.1.0",
            deploy_status="R",
            deploy_commit_hash="abc",
        ),
    )

    assert cli.main(["install", str(package), "--connect", "user/pass@db"]) == 2

    err = capsys.readouterr().err
    assert "DEMO deployment status is R; use resume or reinstall" in err
    assert f"dbpm resume {package}" in err
    assert f"dbpm reinstall {package} --allow-destructive" in err


def test_reinstall_allows_existing_complete_package(tmp_path: Path, monkeypatch):
    package = tmp_path / "package"
    _write_package(package)

    monkeypatch.setattr(
        cli,
        "get_application_state",
        lambda **kwargs: ApplicationState(
            application_name="DEMO",
            version="0.1.0",
            deploy_status="C",
            deploy_commit_hash="abc",
        ),
    )
    monkeypatch.setattr(cli, "get_reverse_dependencies", lambda **kwargs: [])
    monkeypatch.setattr(cli, "execute_plan", lambda *args, **kwargs: 0)
    monkeypatch.setattr(
        cli, "get_core_deployment_metadata",
        lambda **kwargs: DeploymentMetadata(
            deploy_locked="N", deploy_environment="DEV",
            capabilities={
                "DBPM_ALLOW_MUTABLE_SOURCE": "Y",
                "DBPM_ALLOW_SAME_VERSION_REPLACE": "Y",
            },
        ),
    )

    assert (
        cli.main(
            [
                "reinstall",
                str(package),
                "--connect",
                "user/pass@db",
                "--allow-destructive",
            ]
        )
        == 0
    )


def test_reinstall_blocked_when_core_deploy_locked(tmp_path: Path, monkeypatch, capsys):
    package = tmp_path / "package"
    _write_package(package)

    monkeypatch.setattr(
        cli,
        "get_core_deployment_metadata",
        lambda **kwargs: DeploymentMetadata(deploy_locked="Y", deploy_environment="PROD"),
    )
    monkeypatch.setattr(
        cli,
        "get_application_state",
        lambda **kwargs: ApplicationState(
            application_name="DEMO",
            version="0.1.0",
            deploy_status="C",
            deploy_commit_hash="abc",
        ),
    )

    assert (
        cli.main(
            [
                "reinstall",
                str(package),
                "--connect",
                "user/pass@db",
                "--allow-destructive",
            ]
        )
        == 2
    )

    assert "`reinstall` is blocked when DEPLOY_LOCKED=Y" in capsys.readouterr().err


def test_reinstall_allows_incomplete_existing_package(tmp_path: Path, monkeypatch):
    package = tmp_path / "package"
    _write_package(package)

    monkeypatch.setattr(
        cli,
        "get_application_state",
        lambda **kwargs: ApplicationState(
            application_name="DEMO",
            version="0.1.0",
            deploy_status="R",
            deploy_commit_hash="abc",
        ),
    )
    monkeypatch.setattr(cli, "get_reverse_dependencies", lambda **kwargs: [])
    monkeypatch.setattr(cli, "execute_plan", lambda *args, **kwargs: 0)
    monkeypatch.setattr(
        cli, "get_core_deployment_metadata",
        lambda **kwargs: DeploymentMetadata(
            deploy_locked="N", deploy_environment="DEV",
            capabilities={
                "DBPM_ALLOW_MUTABLE_SOURCE": "Y",
                "DBPM_ALLOW_SAME_VERSION_REPLACE": "Y",
            },
        ),
    )

    assert (
        cli.main(
            [
                "reinstall",
                str(package),
                "--connect",
                "user/pass@db",
                "--allow-destructive",
            ]
        )
        == 0
    )


def test_reinstall_blocks_when_dependents_exist(tmp_path: Path, monkeypatch, capsys):
    package = tmp_path / "package"
    _write_package(package)

    monkeypatch.setattr(
        cli,
        "get_application_state",
        lambda **kwargs: ApplicationState(
            application_name="DEMO",
            version="0.1.0",
            deploy_status="C",
            deploy_commit_hash="abc",
        ),
    )
    monkeypatch.setattr(cli, "get_reverse_dependencies", lambda **kwargs: ["JOB_CONTROL", "MY_APP"])
    monkeypatch.setattr(
        cli, "get_core_deployment_metadata",
        lambda **kwargs: DeploymentMetadata(
            deploy_locked="N", deploy_environment="DEV",
            capabilities={
                "DBPM_ALLOW_MUTABLE_SOURCE": "Y",
                "DBPM_ALLOW_SAME_VERSION_REPLACE": "Y",
            },
        ),
    )

    assert (
        cli.main(
            [
                "reinstall",
                str(package),
                "--connect",
                "user/pass@db",
                "--allow-destructive",
            ]
        )
        == 2
    )

    assert (
        "Cannot reinstall DEMO; installed applications depend on it: JOB_CONTROL, MY_APP"
        in capsys.readouterr().err
    )


def _write_core_reinstall_package(path: Path) -> None:
    path.mkdir()
    (path / "dbpm.yaml").write_text(
        """
package:
  name: core
  version: "3.4.0"

scripts:
  install: Deployment_Manifests/deploy.sql
""",
        encoding="utf-8",
    )


def test_core_reinstall_dry_run_shows_delete_system_confirmation(tmp_path: Path, capsys):
    package = tmp_path / "core"
    _write_core_reinstall_package(package)

    assert (
        cli.main(
            [
                "reinstall",
                str(package),
                "--allow-destructive",
                "--dry-run",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["policy"]["result"] == "requires-approval"
    assert "Core reinstall requires --confirm-delete-system CORE" in output["policy"]["required_approvals"]


def test_core_reinstall_blocks_without_delete_system_confirmation(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    package = tmp_path / "core"
    _write_core_reinstall_package(package)

    monkeypatch.setattr(
        cli,
        "get_application_state",
        lambda **kwargs: ApplicationState("CORE", "3.4.0", "C", "abc"),
    )
    monkeypatch.setattr(cli, "execute_plan", lambda *args, **kwargs: pytest.fail("should not execute"))

    assert (
        cli.main(
            [
                "reinstall",
                str(package),
                "--connect",
                "user/pass@db",
                "--allow-destructive",
            ]
        )
        == 2
    )

    assert "Core reinstall requires --confirm-delete-system CORE" in capsys.readouterr().err


def test_core_reinstall_allows_delete_system_confirmation(tmp_path: Path, monkeypatch):
    package = tmp_path / "core"
    _write_core_reinstall_package(package)
    calls = {}

    monkeypatch.setattr(
        cli,
        "get_application_state",
        lambda **kwargs: ApplicationState("CORE", "3.4.0", "C", "abc"),
    )

    def fake_execute_plan(plan, *, connect: str, runner: str, runtime_prefix: str | None = None):
        calls["policy"] = plan["policy"]
        return 0

    monkeypatch.setattr(cli, "execute_plan", fake_execute_plan)

    assert (
        cli.main(
            [
                "reinstall",
                str(package),
                "--connect",
                "user/pass@db",
                "--allow-destructive",
                "--confirm-delete-system",
                "CORE",
            ]
        )
        == 0
    )

    assert calls["policy"]["result"] == "allowed"


def test_resume_allows_running_deployment(tmp_path: Path, monkeypatch):
    package = tmp_path / "package"
    _write_package(package)

    monkeypatch.setattr(
        cli,
        "get_application_state",
        lambda **kwargs: ApplicationState(
            application_name="DEMO",
            version="0.1.0",
            deploy_status="R",
            deploy_commit_hash="abc",
        ),
    )
    monkeypatch.setattr(cli, "execute_plan", lambda *args, **kwargs: 0)

    assert cli.main(["resume", str(package), "--connect", "user/pass@db"]) == 0


def test_resume_as_upgrade_selects_upgrade_script_for_database_only_package(
    tmp_path: Path, monkeypatch
):
    # A database-only package (no application_runtime) has no operation
    # state for resume to infer from; --as lets the user say what was
    # actually in flight.
    package = tmp_path / "package"
    _write_package_with_upgrade(package)

    monkeypatch.setattr(
        cli,
        "get_application_state",
        lambda **kwargs: ApplicationState(
            application_name="DEMO",
            version="1.0.1",
            deploy_status="R",
            deploy_commit_hash="abc",
        ),
    )
    calls = {}

    def fake_execute_plan(plan, *args, **kwargs):
        calls["plan"] = plan
        return 0

    monkeypatch.setattr(cli, "execute_plan", fake_execute_plan)

    assert cli.main(
        ["resume", str(package), "--as", "upgrade", "--connect", "user/pass@db"]
    ) == 0

    assert calls["plan"]["execution"]["script"] == "Deployment_Manifests/upgrade.sql"


def test_resume_without_as_still_defaults_to_install_script(tmp_path: Path, monkeypatch):
    package = tmp_path / "package"
    _write_package_with_upgrade(package)

    monkeypatch.setattr(
        cli,
        "get_application_state",
        lambda **kwargs: ApplicationState(
            application_name="DEMO",
            version="1.0.1",
            deploy_status="R",
            deploy_commit_hash="abc",
        ),
    )
    calls = {}

    def fake_execute_plan(plan, *args, **kwargs):
        calls["plan"] = plan
        return 0

    monkeypatch.setattr(cli, "execute_plan", fake_execute_plan)

    assert cli.main(["resume", str(package), "--connect", "user/pass@db"]) == 0

    assert calls["plan"]["execution"]["script"] == "Deployment_Manifests/deploy.sql"


def test_resume_blocks_complete_deployment(tmp_path: Path, monkeypatch, capsys):
    package = tmp_path / "package"
    _write_package(package)

    monkeypatch.setattr(
        cli,
        "get_application_state",
        lambda **kwargs: ApplicationState(
            application_name="DEMO",
            version="0.1.0",
            deploy_status="C",
            deploy_commit_hash="abc",
        ),
    )

    assert cli.main(["resume", str(package), "--connect", "user/pass@db"]) == 2

    assert "DEMO deployment status is C; resume requires R or F" in capsys.readouterr().err


def test_validate_requires_complete_deployment(tmp_path: Path, monkeypatch):
    package = tmp_path / "package"
    _write_package(package)

    monkeypatch.setattr(
        cli,
        "get_application_state",
        lambda **kwargs: ApplicationState(
            application_name="DEMO",
            version="0.1.0",
            deploy_status="C",
            deploy_commit_hash="abc",
        ),
    )
    monkeypatch.setattr(cli, "execute_plan", lambda *args, **kwargs: 0)

    assert cli.main(["validate", str(package), "--connect", "user/pass@db"]) == 0


def test_validate_with_dependency_source_executes_multi_package_plan(
    tmp_path: Path,
    monkeypatch,
):
    base = tmp_path / "base"
    consumer = tmp_path / "consumer"
    _write_package(base)
    consumer.mkdir()
    (consumer / "dbpm.yaml").write_text(
        """
package:
  name: consumer
  version: "0.1.0"

dependencies:
  - name: demo
    version: "0.1.0"

scripts:
  install: deploy.sql
  validate: smoke.sql
""",
        encoding="utf-8",
    )
    calls = {}

    def fake_get_application_state(**kwargs):
        return ApplicationState(
            application_name=kwargs["application_name"],
            version="0.1.0",
            deploy_status="C",
            deploy_commit_hash="abc",
        )

    def fake_execute_plan(plan, *, connect: str, runner: str, runtime_prefix: str | None = None):
        calls["plan"] = plan
        calls["connect"] = connect
        calls["runner"] = runner
        return 0

    monkeypatch.setattr(cli, "get_application_state", fake_get_application_state)
    monkeypatch.setattr(cli, "execute_plan", fake_execute_plan)

    assert (
        cli.main(
            [
                "validate",
                str(consumer),
                "--dependency-source",
                str(base),
                "--connect",
                "user/pass@db",
            ]
        )
        == 0
    )

    assert calls["connect"] == "user/pass@db"
    assert calls["plan"]["schema_version"] == "dbpm.multi-plan.v0"
    assert calls["plan"]["execution_order"] == ["DEMO", "CONSUMER"]
    assert [item["mode"] for item in calls["plan"]["packages"]] == ["validate", "validate"]


def test_validate_blocks_running_deployment(tmp_path: Path, monkeypatch, capsys):
    package = tmp_path / "package"
    _write_package(package)

    monkeypatch.setattr(
        cli,
        "get_application_state",
        lambda **kwargs: ApplicationState(
            application_name="DEMO",
            version="0.1.0",
            deploy_status="R",
            deploy_commit_hash="abc",
        ),
    )

    assert cli.main(["validate", str(package), "--connect", "user/pass@db"]) == 2

    assert "DEMO deployment status is R; validate requires C" in capsys.readouterr().err


def _write_package_with_upgrade(path: Path) -> None:
    path.mkdir()
    (path / "dbpm.yaml").write_text(
        """
package:
  name: demo
  version: "1.0.1"

scripts:
  install: Deployment_Manifests/deploy.sql
  upgrade: Deployment_Manifests/upgrade.sql
  validate: Tests/smoke_test.sql
""",
        encoding="utf-8",
    )


def _write_core_package_with_upgrade(path: Path) -> None:
    path.mkdir()
    (path / "dbpm.yaml").write_text(
        """
package:
  name: core
  version: "3.3.0"

scripts:
  install: Deployment_Manifests/deploy.sql
  upgrade: Deployment_Manifests/update.sql
""",
        encoding="utf-8",
    )


def test_upgrade_allows_complete_installed_package(tmp_path: Path, monkeypatch):
    package = tmp_path / "package"
    _write_package_with_upgrade(package)

    monkeypatch.setattr(
        cli,
        "get_application_state",
        lambda **kwargs: ApplicationState(
            application_name="DEMO",
            version="1.0.0",
            deploy_status="C",
            deploy_commit_hash="abc",
        ),
    )
    monkeypatch.setattr(cli, "execute_plan", lambda *args, **kwargs: 0)

    assert cli.main(["upgrade", str(package), "--connect", "user/pass@db"]) == 0


def test_core_upgrade_reads_installed_state_and_executes(tmp_path: Path, monkeypatch):
    package = tmp_path / "core"
    _write_core_package_with_upgrade(package)
    calls = {}

    def fake_get_application_state(**kwargs):
        calls["state_application_name"] = kwargs["application_name"]
        return ApplicationState(
            application_name="CORE",
            version="3.2.0",
            deploy_status="C",
            deploy_commit_hash="abc",
        )

    def fake_execute_plan(plan, *, connect: str, runner: str, runtime_prefix: str | None = None):
        calls["plan"] = plan
        calls["connect"] = connect
        calls["runner"] = runner
        return 0

    monkeypatch.setattr(cli, "get_application_state", fake_get_application_state)
    monkeypatch.setattr(cli, "execute_plan", fake_execute_plan)

    assert cli.main(["upgrade", str(package), "--connect", "user/pass@db"]) == 0

    assert calls["state_application_name"] == "CORE"
    assert calls["connect"] == "user/pass@db"
    assert calls["plan"]["installed_state"]["version"] == "3.2.0"
    assert calls["plan"]["pre_actions"][0]["type"] == "stage_deployment_provenance"


def test_upgrade_with_dependency_source_executes_multi_package_plan(
    tmp_path: Path,
    monkeypatch,
):
    base = tmp_path / "base"
    consumer = tmp_path / "consumer"
    _write_package_with_upgrade(base)
    consumer.mkdir()
    (consumer / "dbpm.yaml").write_text(
        """
package:
  name: consumer
  version: "1.0.1"

dependencies:
  - name: demo
    version: "^1.0.0"

scripts:
  install: deploy.sql
  upgrade: upgrade.sql
  validate: smoke.sql
""",
        encoding="utf-8",
    )
    calls = {}

    def fake_get_application_state(**kwargs):
        version = "1.0.0"
        return ApplicationState(
            application_name=kwargs["application_name"],
            version=version,
            deploy_status="C",
            deploy_commit_hash="abc",
        )

    def fake_execute_plan(plan, *, connect: str, runner: str, runtime_prefix: str | None = None):
        calls["plan"] = plan
        calls["connect"] = connect
        calls["runner"] = runner
        return 0

    monkeypatch.setattr(cli, "get_application_state", fake_get_application_state)
    monkeypatch.setattr(cli, "execute_plan", fake_execute_plan)

    assert (
        cli.main(
            [
                "upgrade",
                str(consumer),
                "--dependency-source",
                str(base),
                "--connect",
                "user/pass@db",
            ]
        )
        == 0
    )

    assert calls["connect"] == "user/pass@db"
    assert calls["plan"]["schema_version"] == "dbpm.multi-plan.v0"
    assert calls["plan"]["execution_order"] == ["DEMO", "CONSUMER"]
    assert [item["mode"] for item in calls["plan"]["packages"]] == ["upgrade", "upgrade"]


def test_upgrade_with_missing_dependency_source_fails_instead_of_installing(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    (consumer / "dbpm.yaml").write_text(
        """
package:
  name: consumer
  version: "1.0.1"

dependencies:
  - name: demo
    version: "1.0.1"

scripts:
  install: deploy.sql
  upgrade: upgrade.sql
""",
        encoding="utf-8",
    )

    def fake_get_application_state(**kwargs):
        if kwargs["application_name"] == "CONSUMER":
            return ApplicationState(
                application_name="CONSUMER",
                version="1.0.0",
                deploy_status="C",
                deploy_commit_hash="abc",
            )
        return None

    monkeypatch.setattr(cli, "get_application_state", fake_get_application_state)

    assert cli.main(["upgrade", str(consumer), "--connect", "user/pass@db"]) == 2

    assert "Missing dependency source for CONSUMER: DEMO 1.0.1" in capsys.readouterr().err


def test_upgrade_with_uninstalled_dependency_source_fails_instead_of_installing(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    base = tmp_path / "base"
    consumer = tmp_path / "consumer"
    _write_package_with_upgrade(base)
    consumer.mkdir()
    (consumer / "dbpm.yaml").write_text(
        """
package:
  name: consumer
  version: "1.0.1"

dependencies:
  - name: demo
    version: "^1.0.0"

scripts:
  install: deploy.sql
  upgrade: upgrade.sql
""",
        encoding="utf-8",
    )

    def fake_get_application_state(**kwargs):
        if kwargs["application_name"] == "CONSUMER":
            return ApplicationState(
                application_name="CONSUMER",
                version="1.0.0",
                deploy_status="C",
                deploy_commit_hash="abc",
            )
        return None

    monkeypatch.setattr(cli, "get_application_state", fake_get_application_state)

    assert (
        cli.main(
            [
                "upgrade",
                str(consumer),
                "--dependency-source",
                str(base),
                "--connect",
                "user/pass@db",
            ]
        )
        == 2
    )

    assert "Cannot upgrade dependency DEMO; it is not installed; use install first" in capsys.readouterr().err


def test_upgrade_blocks_when_not_installed(tmp_path: Path, monkeypatch, capsys):
    package = tmp_path / "package"
    _write_package_with_upgrade(package)

    monkeypatch.setattr(cli, "get_application_state", lambda **kwargs: None)

    assert cli.main(["upgrade", str(package), "--connect", "user/pass@db"]) == 2

    assert "DEMO is not installed; use install" in capsys.readouterr().err


def test_upgrade_blocks_incomplete_deployment(tmp_path: Path, monkeypatch, capsys):
    package = tmp_path / "package"
    _write_package_with_upgrade(package)

    monkeypatch.setattr(
        cli,
        "get_application_state",
        lambda **kwargs: ApplicationState(
            application_name="DEMO",
            version="1.0.0",
            deploy_status="R",
            deploy_commit_hash="abc",
        ),
    )

    assert cli.main(["upgrade", str(package), "--connect", "user/pass@db"]) == 2

    assert "DEMO deployment status is R; upgrade requires C" in capsys.readouterr().err


def test_upgrade_blocks_same_version(tmp_path: Path, monkeypatch, capsys):
    package = tmp_path / "package"
    _write_package_with_upgrade(package)

    monkeypatch.setattr(
        cli,
        "get_application_state",
        lambda **kwargs: ApplicationState(
            application_name="DEMO",
            version="1.0.1",
            deploy_status="C",
            deploy_commit_hash="abc",
        ),
    )

    assert cli.main(["upgrade", str(package), "--connect", "user/pass@db"]) == 2

    assert "DEMO version 1.0.1 is already installed; no upgrade needed" in capsys.readouterr().err


def test_upgrade_blocks_downgrade(tmp_path: Path, monkeypatch, capsys):
    package = tmp_path / "package"
    _write_package_with_upgrade(package)

    monkeypatch.setattr(
        cli,
        "get_application_state",
        lambda **kwargs: ApplicationState(
            application_name="DEMO",
            version="2.0.0",
            deploy_status="C",
            deploy_commit_hash="abc",
        ),
    )

    assert cli.main(["upgrade", str(package), "--connect", "user/pass@db"]) == 2

    assert "Cannot downgrade DEMO from 2.0.0 to 1.0.1" in capsys.readouterr().err


def test_upgrade_dry_run_prints_plan(tmp_path: Path, capsys):
    package = tmp_path / "package"
    _write_package_with_upgrade(package)

    assert cli.main(["upgrade", str(package), "--dry-run"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "upgrade"


def test_check_core_uses_environment_connect_and_runner(monkeypatch, capsys):
    calls = {}

    def fake_check_core(*, connect: str, runner: str, minimum_version: str | None):
        calls["connect"] = connect
        calls["runner"] = runner
        calls["minimum_version"] = minimum_version
        return SqlResult(returncode=0, stdout="CORE_VERSION=3.0.0\n", stderr="")

    monkeypatch.setenv("DBPM_CONNECT", "user/password@db")
    monkeypatch.setenv("DBPM_SQL_RUNNER", "sql")
    monkeypatch.setattr(cli, "check_core", fake_check_core)

    assert cli.main(["check-core", "--minimum-version", "3.0.0"]) == 0

    assert calls == {
        "connect": "user/password@db",
        "runner": "sql",
        "minimum_version": "3.0.0",
    }
    assert "CORE_VERSION=3.0.0" in capsys.readouterr().out


def test_check_core_uses_environment_connect_name(monkeypatch, capsys):
    calls = {}

    def fake_check_core(*, connect, runner: str, minimum_version: str | None):
        calls["connect"] = connect
        calls["runner"] = runner
        calls["minimum_version"] = minimum_version
        return SqlResult(returncode=0, stdout="CORE_VERSION=3.0.0\n", stderr="")

    monkeypatch.delenv("DBPM_CONNECT", raising=False)
    monkeypatch.setenv("DBPM_CONNECT_NAME", "Development Database (APP_USER)")
    monkeypatch.setenv("DBPM_SQL_RUNNER", "sql")
    monkeypatch.setattr(cli, "check_core", fake_check_core)

    assert cli.main(["check-core"]) == 0

    assert calls["connect"].kind == "sqlcl-name"
    assert calls["connect"].value == "Development Database (APP_USER)"
    assert calls["runner"] == "sql"


def test_check_core_composes_environment_database_credentials(monkeypatch, capsys):
    calls = {}

    def fake_check_core(*, connect: str, runner: str, minimum_version: str | None):
        calls["connect"] = connect
        return SqlResult(returncode=0, stdout="CORE_VERSION=3.0.0\n", stderr="")

    monkeypatch.setenv("DBPM_DB_USER", "app_user")
    monkeypatch.setenv("DBPM_DB_PASSWORD", "app_password")
    monkeypatch.setenv("DBPM_DB_DSN", "db/service")
    monkeypatch.setattr(cli, "check_core", fake_check_core)

    assert cli.main(["check-core"]) == 0

    assert calls["connect"] == "app_user/app_password@db/service"


def test_incomplete_environment_database_credentials_fail(monkeypatch, capsys):
    monkeypatch.setenv("DBPM_DB_USER", "app_user")
    monkeypatch.setenv("DBPM_DB_DSN", "db/service")

    assert cli.main(["check-core"]) == 2

    assert "set DBPM_DB_PASSWORD" in capsys.readouterr().err


def test_raw_and_structured_environment_connections_are_mutually_exclusive(
    monkeypatch, capsys
):
    monkeypatch.setenv("DBPM_CONNECT", "user/password@db")
    monkeypatch.setenv("DBPM_DB_USER", "app_user")
    monkeypatch.setenv("DBPM_DB_PASSWORD", "app_password")
    monkeypatch.setenv("DBPM_DB_DSN", "db/service")

    assert cli.main(["check-core"]) == 2

    assert "Database connection inputs are mutually exclusive" in capsys.readouterr().err


def test_connect_name_and_connect_are_mutually_exclusive(monkeypatch, capsys):
    monkeypatch.setenv("DBPM_CONNECT", "user/password@db")
    monkeypatch.setenv("DBPM_CONNECT_NAME", "Development Database (APP_USER)")
    monkeypatch.setenv("DBPM_SQL_RUNNER", "sql")

    assert cli.main(["check-core"]) == 2

    err = capsys.readouterr().err
    assert "Database connection inputs are mutually exclusive" in err
    assert "raw Oracle connect string" in err
    assert "structured database credentials" in err
    assert "SQLcl saved connection" in err


def test_cli_connect_name_overrides_environment_connect_name(monkeypatch, capsys):
    calls = {}

    def fake_check_core(*, connect, runner: str, minimum_version: str | None):
        calls["connect"] = connect
        return SqlResult(returncode=0, stdout="CORE_VERSION=3.0.0\n", stderr="")

    monkeypatch.delenv("DBPM_CONNECT", raising=False)
    monkeypatch.setenv("DBPM_CONNECT_NAME", "Development Database (OLD)")
    monkeypatch.setenv("DBPM_SQL_RUNNER", "sql")
    monkeypatch.setattr(cli, "check_core", fake_check_core)

    assert cli.main(["check-core", "--connect-name", "Development Database (APP_USER)"]) == 0

    assert calls["connect"].value == "Development Database (APP_USER)"


def test_connect_name_with_default_sqlplus_fails(monkeypatch, capsys):
    monkeypatch.delenv("DBPM_CONNECT", raising=False)
    monkeypatch.delenv("DBPM_SQL_RUNNER", raising=False)
    monkeypatch.setenv("DBPM_CONNECT_NAME", "Development Database (APP_USER)")

    assert cli.main(["check-core"]) == 2

    assert "SQLcl saved connections require a SQLcl runner" in capsys.readouterr().err


# ── upgrade chain ────────────────────────────────────────────────────────────


def _write_maven_upgrade_package(path: Path, *, version: str, upgrade_from: str | None = None) -> None:
    from zipfile import ZipFile

    upgrade_from_line = f"  upgrade_from: \"{upgrade_from}\"\n" if upgrade_from else ""
    manifest = (
        f"package:\n  name: demo\n  version: \"{version}\"\n"
        f"scripts:\n  install: deploy.sql\n  upgrade: upgrade.sql\n{upgrade_from_line}"
    )
    with ZipFile(path, "w") as archive:
        archive.writestr(f"demo/dbpm.yaml", manifest)
        archive.writestr(f"demo/upgrade.sql", "PROMPT upgrade\n")
        archive.writestr(f"demo/deploy.sql", "PROMPT deploy\n")


def test_major_upgrade_with_dependents_is_blocked(tmp_path: Path, monkeypatch, capsys):
    package = tmp_path / "package"
    _write_package_with_upgrade(package)

    monkeypatch.setattr(
        cli,
        "get_application_state",
        lambda **kwargs: ApplicationState("DEMO", "1.0.0", "C", "abc"),
    )
    monkeypatch.setattr(cli, "get_reverse_dependencies", lambda **kwargs: ["CONSUMER"])

    assert cli.main(["upgrade", str(package), "--connect", "user/pass@db"]) == 2

    err = capsys.readouterr().err
    assert "Cannot upgrade DEMO from 1.0.0 to 1.0.1" not in err  # minor bump — would not fire
    # rewrite package with major bump
    (package / "dbpm.yaml").write_text(
        """
package:
  name: demo
  version: "2.0.0"
scripts:
  install: Deployment_Manifests/deploy.sql
  upgrade: Deployment_Manifests/upgrade.sql
""",
        encoding="utf-8",
    )

    assert cli.main(["upgrade", str(package), "--connect", "user/pass@db"]) == 2
    err = capsys.readouterr().err
    assert "Cannot upgrade DEMO from 1.0.0 to 2.0.0" in err
    assert "CONSUMER" in err
    assert "--allow-dependent-break" in err


def test_major_upgrade_allow_dependent_break_proceeds(tmp_path: Path, monkeypatch):
    package = tmp_path / "package"
    package.mkdir()
    (package / "dbpm.yaml").write_text(
        """
package:
  name: demo
  version: "2.0.0"
scripts:
  install: Deployment_Manifests/deploy.sql
  upgrade: Deployment_Manifests/upgrade.sql
""",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        cli,
        "get_application_state",
        lambda **kwargs: ApplicationState("DEMO", "1.0.0", "C", "abc"),
    )
    monkeypatch.setattr(cli, "get_reverse_dependencies", lambda **kwargs: ["CONSUMER"])
    monkeypatch.setattr(cli, "execute_plan", lambda *a, **kw: 0)

    assert cli.main(["upgrade", str(package), "--connect", "user/pass@db", "--allow-dependent-break"]) == 0


def test_minor_upgrade_with_dependents_is_not_blocked(tmp_path: Path, monkeypatch):
    package = tmp_path / "package"
    _write_package_with_upgrade(package)

    monkeypatch.setattr(
        cli,
        "get_application_state",
        lambda **kwargs: ApplicationState("DEMO", "1.0.0", "C", "abc"),
    )
    monkeypatch.setattr(cli, "get_reverse_dependencies", lambda **kwargs: ["CONSUMER"])
    monkeypatch.setattr(cli, "execute_plan", lambda *a, **kw: 0)

    assert cli.main(["upgrade", str(package), "--connect", "user/pass@db"]) == 0


def test_major_upgrade_with_dependents_is_blocked_for_multi_package_plan(
    tmp_path: Path, monkeypatch, capsys
):
    dep = tmp_path / "dep"
    consumer = tmp_path / "consumer"
    _write_package_with_upgrade(dep)
    consumer.mkdir()
    (consumer / "dbpm.yaml").write_text(
        """
package:
  name: consumer
  version: "2.0.0"

dependencies:
  - name: demo
    version: "^1.0.0"

scripts:
  install: deploy.sql
  upgrade: upgrade.sql
""",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        cli,
        "get_application_state",
        lambda **kwargs: ApplicationState(kwargs["application_name"], "1.0.0", "C", "abc"),
    )

    def fake_reverse_deps(**kwargs):
        if kwargs.get("application_name") == "CONSUMER":
            return ["DOWNSTREAM"]
        return []

    monkeypatch.setattr(cli, "get_reverse_dependencies", fake_reverse_deps)

    assert (
        cli.main(
            [
                "upgrade",
                str(consumer),
                "--dependency-source",
                str(dep),
                "--connect",
                "user/pass@db",
            ]
        )
        == 2
    )

    err = capsys.readouterr().err
    assert "Cannot upgrade CONSUMER from 1.0.0 to 2.0.0" in err
    assert "DOWNSTREAM" in err


# ---------------------------------------------------------------------------
# Core minimum version preflight
# ---------------------------------------------------------------------------


def _write_package_requiring_core(path: Path, core_version: str = "3.4.0") -> None:
    path.mkdir()
    (path / "dbpm.yaml").write_text(
        f"""
package:
  name: demo
  version: "1.0.1"

core:
  minimum_version: "{core_version}"

scripts:
  install: deploy.sql
  upgrade: upgrade.sql
""",
        encoding="utf-8",
    )


def test_install_blocked_when_core_too_old(tmp_path: Path, monkeypatch, capsys):
    package = tmp_path / "package"
    _write_package_requiring_core(package, core_version="3.4.0")

    monkeypatch.setattr(cli, "get_application_state", lambda **kwargs: (
        None if kwargs["application_name"] != "CORE"
        else ApplicationState("CORE", "3.2.0", "C", "abc")
    ))

    assert cli.main(["install", str(package), "--connect", "user/pass@db"]) == 2

    err = capsys.readouterr().err
    assert "Core 3.4.0" in err
    assert "3.2.0" in err
    assert "Upgrade Core first" in err


def test_install_proceeds_when_core_meets_requirement(tmp_path: Path, monkeypatch):
    package = tmp_path / "package"
    _write_package_requiring_core(package, core_version="3.2.0")

    monkeypatch.setattr(cli, "get_application_state", lambda **kwargs: (
        None if kwargs["application_name"] != "CORE"
        else ApplicationState("CORE", "3.4.0", "C", "abc")
    ))
    monkeypatch.setattr(cli, "execute_plan", lambda *a, **kw: 0)

    assert cli.main(["install", str(package), "--connect", "user/pass@db"]) == 0


def test_install_blocked_when_core_not_installed(tmp_path: Path, monkeypatch, capsys):
    package = tmp_path / "package"
    _write_package_requiring_core(package, core_version="3.4.0")

    monkeypatch.setattr(cli, "get_application_state", lambda **kwargs: None)

    assert cli.main(["install", str(package), "--connect", "user/pass@db"]) == 2

    err = capsys.readouterr().err
    assert "Core is not installed" in err
    assert "bootstrap-core" in err


def test_install_blocked_when_core_deployment_is_not_complete(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    package = tmp_path / "package"
    _write_package_requiring_core(package, core_version="3.4.0")

    monkeypatch.setattr(cli, "get_application_state", lambda **kwargs: (
        None if kwargs["application_name"] != "CORE"
        else ApplicationState("CORE", "3.4.0", "F", "abc")
    ))

    assert cli.main(["install", str(package), "--connect", "user/pass@db"]) == 2

    err = capsys.readouterr().err
    assert "Core deployment status is F" in err
    assert "resume or reinstall Core" in err


def _write_core_bootstrap_package(path: Path) -> None:
    path.mkdir()
    (path / "dbpm.yaml").write_text(
        """
package:
  name: core
  version: "3.4.0"

scripts:
  install: Deployment_Manifests/deploy.sql
""",
        encoding="utf-8",
    )


def test_bootstrap_core_blocks_when_core_is_already_installed(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    package = tmp_path / "core"
    _write_core_bootstrap_package(package)

    monkeypatch.setattr(
        cli,
        "get_application_state",
        lambda **kwargs: ApplicationState("CORE", "3.4.0", "C", "abc"),
    )

    def fail_execute_plan(*args, **kwargs):
        raise AssertionError("bootstrap-core should not execute when Core is already installed")

    monkeypatch.setattr(cli, "execute_plan", fail_execute_plan)

    assert cli.main(["bootstrap-core", str(package), "--connect", "user/pass@db"]) == 2

    err = capsys.readouterr().err
    assert "CORE is already installed with status C" in err
    assert "instead of bootstrap-core" in err


def test_bootstrap_core_runs_when_core_is_not_installed(tmp_path: Path, monkeypatch):
    package = tmp_path / "core"
    _write_core_bootstrap_package(package)
    calls = {}

    def fake_get_application_state(**kwargs):
        calls["application_name"] = kwargs["application_name"]
        return None

    def fake_execute_plan(plan, *, connect: str, runner: str, runtime_prefix: str | None = None):
        calls["plan"] = plan
        return 0

    monkeypatch.setattr(cli, "get_application_state", fake_get_application_state)
    monkeypatch.setattr(cli, "execute_plan", fake_execute_plan)

    assert cli.main(["bootstrap-core", str(package), "--connect", "user/pass@db"]) == 0

    assert calls["application_name"] == "CORE"
    assert calls["plan"]["installed_state"] is None


def test_bootstrap_core_accepts_policy_and_deploy_environment(tmp_path: Path, monkeypatch):
    package = tmp_path / "core"
    _write_core_bootstrap_package(package)
    calls = {}

    monkeypatch.setattr(cli, "get_application_state", lambda **kwargs: None)

    def fake_execute_plan(plan, *, connect: str, runner: str, runtime_prefix: str | None = None):
        calls["plan"] = plan
        return 0

    monkeypatch.setattr(cli, "execute_plan", fake_execute_plan)

    assert (
        cli.main(
            [
                "bootstrap-core",
                str(package),
                "--connect",
                "user/pass@db",
                "--policy",
                "locked",
                "--deploy-environment",
                "PROD",
            ]
        )
        == 0
    )

    assert calls["plan"]["policy"]["policy_context"] == {
        "deployment_locked": True,
        "source": "cli-policy",
        "deploy_environment": "PROD",
    }
    assert calls["plan"]["execution"]["stdin"] == "Y\nPROD\n"


def test_bootstrap_core_skips_core_version_check(tmp_path: Path, monkeypatch):
    package = tmp_path / "package"
    _write_package_requiring_core(package, core_version="3.4.0")

    monkeypatch.setattr(cli, "get_application_state", lambda **kwargs: None)
    monkeypatch.setattr(cli, "execute_plan", lambda *a, **kw: 0)

    assert cli.main(["bootstrap-core", str(package), "--connect", "user/pass@db"]) == 0


def _version_aware_cli_download(tmp_path: Path, name: str):
    import re

    def _download(url: str, dest: Path) -> None:
        match = re.search(r"/(\d+\.\d+\.\d+)/", url)
        version = match.group(1) if match else "1.0.0"
        buf = tmp_path / f"_buf_{version}.zip"
        _write_maven_upgrade_package(buf, version=version)
        dest.write_bytes(buf.read_bytes())

    return _download


def test_upgrade_chain_dry_run_outputs_chain_plan(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("DBPM_CACHE_DIR", str(tmp_path / "cache"))

    monkeypatch.setattr(
        "dbpm.chain._maven_version_list",
        lambda repo, coord: ["1.0.0", "1.1.0", "1.2.0", "1.3.0"],
    )
    monkeypatch.setattr("dbpm.source._download", _version_aware_cli_download(tmp_path, "demo"))
    monkeypatch.setattr(
        cli,
        "get_application_state",
        lambda **kwargs: ApplicationState(
            application_name="DEMO",
            version="1.0.0",
            deploy_status="C",
            deploy_commit_hash="abc",
        ),
    )

    raw = "gh-maven:rsantmyer/demo:com.example:demo:1.3.0"
    assert cli.main(["upgrade", raw, "--dry-run", "--connect", "user/pass@db"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["schema_version"] == "dbpm.upgrade-chain.v0"
    assert output["installed_version"] == "1.0.0"
    assert len(output["steps"]) == 3
    assert [s["package"]["version"] for s in output["steps"]] == ["1.1.0", "1.2.0", "1.3.0"]


def test_upgrade_chain_maven_with_satisfied_upgrade_from_is_direct(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("DBPM_CACHE_DIR", str(tmp_path / "cache"))

    def _download(url: str, dest: Path) -> None:
        buf = tmp_path / "_buf_1.3.0.zip"
        _write_maven_upgrade_package(buf, version="1.3.0", upgrade_from="^1.0.0")
        dest.write_bytes(buf.read_bytes())

    monkeypatch.setattr("dbpm.source._download", _download)
    monkeypatch.setattr(
        cli,
        "get_application_state",
        lambda **kwargs: ApplicationState(
            application_name="DEMO",
            version="1.2.0",
            deploy_status="C",
            deploy_commit_hash="abc",
        ),
    )
    monkeypatch.setattr(cli, "execute_plan", lambda *a, **kw: 0)

    raw = "gh-maven:rsantmyer/demo:com.example:demo:1.3.0"
    assert cli.main(["upgrade", raw, "--connect", "user/pass@db", "--verbose"]) == 0

    out = capsys.readouterr()
    assert out.err == (
        "dbpm: Preparing upgrade plan...\n"
        "dbpm: Loading root package source...\n"
        "dbpm: Resolving package provenance...\n"
        "dbpm: Reading Core deployment policy...\n"
        "dbpm: Reading installed state for DEMO...\n"
        "dbpm: Reading reverse dependencies for DEMO...\n"
        "dbpm: Checking demo 1.3.0...\n"
        "dbpm: Upgrade completed successfully: demo 1.3.0\n"
    )


def test_upgrade_chain_executes_steps_in_order(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DBPM_CACHE_DIR", str(tmp_path / "cache"))

    versions_returned = ["1.0.0"]

    def fake_get_state(**kwargs):
        return ApplicationState(
            application_name="DEMO",
            version=versions_returned[-1],
            deploy_status="C",
            deploy_commit_hash="abc",
        )

    executed_versions = []

    def fake_execute_plan(plan, *, connect, runner, runtime_prefix=None):
        package = plan.get("package", {})
        executed_versions.append(package.get("version"))
        versions_returned.append(package.get("version"))

    monkeypatch.setattr(
        "dbpm.chain._maven_version_list",
        lambda repo, coord: ["1.0.0", "1.1.0", "1.2.0", "1.3.0"],
    )
    monkeypatch.setattr("dbpm.source._download", _version_aware_cli_download(tmp_path, "demo"))
    monkeypatch.setattr(cli, "get_application_state", fake_get_state)
    monkeypatch.setattr(cli, "execute_plan", fake_execute_plan)

    raw = "gh-maven:rsantmyer/demo:com.example:demo:1.3.0"
    assert cli.main(["upgrade", raw, "--connect", "user/pass@db"]) == 0

    assert executed_versions == ["1.1.0", "1.2.0", "1.3.0"]


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------


def _write_publish_package(path: Path) -> None:
    path.mkdir()
    (path / "dbpm.yaml").write_text(
        """
package:
  name: demo
  version: "0.1.0"

publish:
  group: com.example.database
  artifact_id: demo

scripts:
  install: Deployment_Manifests/deploy.sql
""",
        encoding="utf-8",
    )


def test_publish_dry_run(tmp_path: Path, capsys, monkeypatch):
    package = tmp_path / "package"
    _write_publish_package(package)
    monkeypatch.setenv("DBPM_CACHE_DIR", str(tmp_path / "cache"))

    ret = cli.main([
        "publish",
        str(package),
        "--target", "gh-maven:acme/myrepo",
        "--signing-key", "test@example.com",
        "--dry-run",
    ])

    assert ret == 0
    out = capsys.readouterr().out
    assert "DRY_RUN" in out
    assert "demo-0.1.0.zip" in out
    assert "demo-0.1.0.pom" in out
    assert "gh-maven:acme/myrepo" in out


def test_publish_requires_target(tmp_path: Path):
    package = tmp_path / "package"
    _write_publish_package(package)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["publish", str(package), "--signing-key", "key"])

    assert exc_info.value.code != 0


def test_publish_missing_signing_key_fails(tmp_path: Path, capsys, monkeypatch):
    package = tmp_path / "package"
    _write_publish_package(package)
    monkeypatch.delenv("DBPM_SIGNING_KEY", raising=False)
    monkeypatch.setenv("DBPM_CACHE_DIR", str(tmp_path / "cache"))

    ret = cli.main(["publish", str(package), "--target", "gh-maven:acme/myrepo"])

    assert ret == 2
    assert "signing key" in capsys.readouterr().err.lower()


def test_publish_no_publish_config_fails(tmp_path: Path, capsys, monkeypatch):
    package = tmp_path / "package"
    package.mkdir()
    (package / "dbpm.yaml").write_text(
        "package:\n  name: demo\n  version: '0.1.0'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DBPM_CACHE_DIR", str(tmp_path / "cache"))

    ret = cli.main([
        "publish",
        str(package),
        "--target", "gh-maven:acme/myrepo",
        "--signing-key", "key",
    ])

    assert ret == 2
    err = capsys.readouterr().err
    assert "publish" in err.lower()


def test_publish_group_override(tmp_path: Path, capsys, monkeypatch):
    package = tmp_path / "package"
    package.mkdir()
    (package / "dbpm.yaml").write_text(
        "package:\n  name: demo\n  version: '0.1.0'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DBPM_CACHE_DIR", str(tmp_path / "cache"))

    ret = cli.main([
        "publish",
        str(package),
        "--target", "gh-maven:acme/myrepo",
        "--group", "com.override",
        "--signing-key", "key",
        "--dry-run",
    ])

    assert ret == 0
    out = capsys.readouterr().out
    assert "com.override" in out


def test_publish_cli_overrides_reach_artifact_build(tmp_path: Path, monkeypatch):
    package = tmp_path / "package"
    _write_publish_package(package)
    artifact_path = tmp_path / "artifact.zip"
    artifact_path.write_bytes(b"zip")
    captured = {}

    def fake_build_artifact(source_path, manifest, publish_config):
        captured["source_path"] = source_path
        captured["group"] = publish_config.group
        captured["artifact_id"] = publish_config.artifact_id
        return artifact_path

    class Receipt:
        artifact_url = "https://example.test/demo.zip"
        checksum = "abc123"
        signature_url = "https://example.test/demo.zip.asc"

    monkeypatch.setattr(cli, "build_artifact", fake_build_artifact)
    monkeypatch.setattr(cli, "publish_to_repository", lambda *args: Receipt())
    monkeypatch.setattr(cli, "verify_publish", lambda *args: None)
    monkeypatch.setattr(cli, "resolve_signing_key_fingerprint", lambda key: "FINGERPRINT")

    ret = cli.main([
        "publish",
        str(package),
        "--target", "gh-maven:acme/myrepo",
        "--group", "com.override",
        "--artifact-id", "core",
        "--signing-key", "key",
    ])

    assert ret == 0
    assert captured["source_path"] == package
    assert captured["group"] == "com.override"
    assert captured["artifact_id"] == "core"


def _write_indexable_package(path: Path) -> None:
    path.mkdir()
    (path / "dbpm.yaml").write_text(
        """
package:
  name: demo
  version: "1.2.3"
  description: Demo package
  vendor: acme
database:
  minimum_version: "19c"
core:
  minimum_version: "3.4.0"
dependencies:
  - name: utl_interval
    version: "^1.0.0"
""",
        encoding="utf-8",
    )
    (path / "dbpm-publish-receipt.json").write_text(
        json.dumps(
            {
                "schema_version": "dbpm.publish-receipt.v1",
                "package": {"name": "demo", "version": "1.2.3"},
                "artifact": {
                    "url": "https://repo.example/demo-1.2.3.zip",
                    "checksum": "sha256:" + "a" * 64,
                    "signature_url": "https://repo.example/demo-1.2.3.zip.asc",
                    "publisher_key_fingerprint": "FINGERPRINT",
                },
                "published_at": "2026-06-04T12:00:00Z",
            }
        ),
        encoding="utf-8",
    )


def test_registry_index_dry_run_prints_secret_free_payload(tmp_path: Path, capsys, monkeypatch):
    package = tmp_path / "package"
    _write_indexable_package(package)
    monkeypatch.setenv("DBPM_REGISTRY_TOKEN", "top-secret")

    ret = cli.main(["registry", "index", str(package), "--dry-run"])

    assert ret == 0
    output = capsys.readouterr().out
    assert '"status": "active"' in output
    assert '"constraint": "^1.0.0"' in output
    assert "top-secret" not in output


def test_registry_index_requires_token_before_post(tmp_path: Path, capsys, monkeypatch):
    package = tmp_path / "package"
    _write_indexable_package(package)
    monkeypatch.delenv("DBPM_REGISTRY_TOKEN", raising=False)
    monkeypatch.setattr(cli, "index_registry_version", lambda *args, **kwargs: pytest.fail("posted"))

    ret = cli.main(["registry", "index", str(package)])

    assert ret == 2
    assert "DBPM_REGISTRY_TOKEN" in capsys.readouterr().err


def test_publish_writes_custom_receipt_and_preserves_output(tmp_path: Path, capsys, monkeypatch):
    package = tmp_path / "package"
    _write_publish_package(package)
    output = tmp_path / "release" / "receipt.json"
    artifact_path = tmp_path / "artifact.zip"
    artifact_path.write_bytes(b"zip")
    receipt = cli.PublishReceipt(
        artifact_url="https://repo.example/demo.zip",
        checksum="a" * 64,
        signature_url="https://repo.example/demo.zip.asc",
    )
    monkeypatch.setattr(cli, "build_artifact", lambda *args: artifact_path)
    monkeypatch.setattr(cli, "publish_to_repository", lambda *args: receipt)
    monkeypatch.setattr(cli, "verify_publish", lambda *args: None)
    monkeypatch.setattr(cli, "resolve_signing_key_fingerprint", lambda key: "FINGERPRINT")

    ret = cli.main(
        [
            "publish",
            str(package),
            "--target",
            "gh-maven:acme/repo",
            "--signing-key",
            "key",
            "--receipt-output",
            str(output),
        ]
    )

    assert ret == 0
    assert output.exists()
    stdout = capsys.readouterr().out
    assert "PUBLISHED=https://repo.example/demo.zip" in stdout
    assert f"WROTE_PUBLISH_RECEIPT={output}" in stdout


def test_publish_index_failure_preserves_receipt_and_returns_failure(tmp_path: Path, capsys, monkeypatch):
    package = tmp_path / "package"
    _write_publish_package(package)
    artifact_path = tmp_path / "artifact.zip"
    artifact_path.write_bytes(b"zip")
    receipt = cli.PublishReceipt(
        artifact_url="https://repo.example/demo.zip",
        checksum="a" * 64,
        signature_url="https://repo.example/demo.zip.asc",
    )
    monkeypatch.setenv("DBPM_REGISTRY_PUBLISHER", "acme")
    monkeypatch.setenv("DBPM_REGISTRY_DESCRIPTION", "Demo package")
    monkeypatch.setenv("DBPM_REGISTRY_TOKEN", "top-secret")
    monkeypatch.setattr(cli, "build_artifact", lambda *args: artifact_path)
    monkeypatch.setattr(cli, "publish_to_repository", lambda *args: receipt)
    monkeypatch.setattr(cli, "verify_publish", lambda *args: None)
    monkeypatch.setattr(cli, "resolve_signing_key_fingerprint", lambda key: "FINGERPRINT")
    monkeypatch.setattr(cli, "index_registry_version", lambda *args, **kwargs: (_ for _ in ()).throw(cli.DbpmError("HTTP 503")))

    ret = cli.main(
        [
            "publish",
            str(package),
            "--target",
            "gh-maven:acme/repo",
            "--signing-key",
            "key",
            "--index-registry",
        ]
    )

    assert ret == 2
    assert (package / "dbpm-publish-receipt.json").exists()
    assert "Publishing succeeded" in capsys.readouterr().err


def test_rollback_cli_checks_database_versions_and_reports_generation(
    tmp_path: Path,
    capsys,
    monkeypatch,
):
    from types import SimpleNamespace

    target = SimpleNamespace(packages=[SimpleNamespace(name="demo")])
    monkeypatch.setattr(
        cli,
        "load_retained_application_runtime_receipt",
        lambda *args, **kwargs: target,
    )
    monkeypatch.setattr(
        cli,
        "_get_installed_state",
        lambda args, app_name: {"version": "1.0.0", "deploy_status": "C"},
    )
    captured = {}

    def fake_rollback(prefix, *, database_versions, target_generation):
        captured["prefix"] = prefix
        captured["versions"] = database_versions
        captured["target"] = target_generation
        return SimpleNamespace(generation=4)

    monkeypatch.setattr(cli, "rollback_application_runtime", fake_rollback)

    result = cli.main(
        [
            "rollback",
            "--runtime-prefix",
            str(tmp_path),
            "--target-generation",
            "2",
            "--connect",
            "user/password@db",
        ]
    )

    assert result == 0
    assert captured["versions"] == {"demo": "1.0.0"}
    assert captured["target"] == 2
    output = capsys.readouterr()
    assert "ROLLED_BACK_RUNTIME_GENERATION=4" in output.out
    assert "dbpm: Runtime rollback completed successfully: generation 4" in output.err


def test_uninstall_cli_exposes_destructive_runtime_options():
    args = cli._build_parser().parse_args(
        [
            "uninstall",
            ".",
            "--runtime-prefix",
            "/opt/demo",
            "--allow-destructive",
        ]
    )

    assert args.command == "uninstall"
    assert args.runtime_prefix == "/opt/demo"
    assert args.allow_destructive is True


def test_resume_accepts_source_free_application_recovery_options():
    args = cli._build_parser().parse_args(
        ["resume", "--application", "DEMO", "--runtime-prefix", "/opt/demo"]
    )
    assert args.source is None
    assert args.application == "DEMO"


def test_runtime_reconcile_replace_fails_closed_until_capability_exists(capsys):
    result = cli.main(
        ["runtime", "reconcile", "--application", "DEMO", "--runtime-prefix", "/opt/demo", "--replace"]
    )
    assert result == 2
    assert "DBPM_ALLOW_RUNTIME_REPLACE" in capsys.readouterr().err


def test_dev_reset_and_reinstall_compile_to_same_plan_digest(tmp_path: Path, monkeypatch, capsys):
    base = tmp_path / "base"
    consumer = tmp_path / "consumer"
    _write_package(base)
    consumer.mkdir()
    (consumer / "dbpm.yaml").write_text(
        """
package:
  name: consumer
  version: "0.1.0"
dependencies:
  - name: demo
    version: "0.1.0"
scripts:
  install: deploy.sql
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli, "get_core_deployment_metadata",
        lambda **kwargs: DeploymentMetadata(
            "N", "DEV", {
                "DBPM_ALLOW_MUTABLE_SOURCE": "Y",
                "DBPM_ALLOW_SAME_VERSION_REPLACE": "Y",
                "DBPM_ALLOW_GRAPH_RESET": "Y",
            },
        ),
    )
    monkeypatch.setattr(
        cli, "get_application_state",
        lambda **kwargs: ApplicationState(kwargs["application_name"], "0.1.0", "C", "abc"),
    )

    common = [
        str(consumer), "--dependency-source", str(base), "--cascade", "graph",
        "--connect", "user/pass@db", "--dry-run",
    ]
    assert cli.main(["reinstall", *common, "--allow-destructive"]) == 0
    reinstall_plan = json.loads(capsys.readouterr().out)
    assert cli.main(["dev", "reset", *common]) == 0
    reset_plan = json.loads(capsys.readouterr().out)

    assert reinstall_plan["plan_digest"] == reset_plan["plan_digest"]
    assert reinstall_plan["audit"]["initiating_surface"] == "reinstall"
    assert reset_plan["audit"]["initiating_surface"] == "dev reset"


def test_environment_reset_keeps_core_and_uses_consumer_first_order(monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "get_core_deployment_metadata",
        lambda **kwargs: DeploymentMetadata(
            "N", "DISPOSABLE", {"DBPM_ALLOW_ENVIRONMENT_RESET": "Y"},
        ),
    )
    monkeypatch.setattr(
        cli, "get_application_state",
        lambda **kwargs: ApplicationState("CORE", "3.5.0", "C", "abc"),
    )
    monkeypatch.setattr(
        cli, "get_installed_application_graph",
        lambda **kwargs: (["CORE", "BASE", "CONSUMER"], [("CONSUMER", "BASE")]),
    )
    captured = {}
    monkeypatch.setattr(cli, "execute_plan", lambda plan, **kwargs: captured.setdefault("plan", plan) or 0)

    assert cli.main([
        "dev", "reset-environment", "--keep", "CORE", "--yes",
        "--confirm", "APP",
        "--connect", "user/pass@db",
    ]) == 0

    assert captured["plan"]["removal_order"] == ["CONSUMER", "BASE"]
    assert captured["plan"]["keep"] == ["CORE"]


def test_environment_reset_refuses_when_core_is_unhealthy(monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "get_core_deployment_metadata",
        lambda **kwargs: DeploymentMetadata(
            "N", "DISPOSABLE", {"DBPM_ALLOW_ENVIRONMENT_RESET": "Y"},
        ),
    )
    monkeypatch.setattr(
        cli, "get_application_state",
        lambda **kwargs: ApplicationState("CORE", "3.5.0", "F", "abc"),
    )

    assert cli.main([
        "dev", "reset-environment", "--keep", "CORE", "--yes",
        "--confirm", "APP", "--connect", "user/pass@db",
    ]) == 2
    assert "CORE is not healthy" in capsys.readouterr().err


def test_environment_reset_requires_confirm_matching_target_even_with_yes(monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "get_core_deployment_metadata",
        lambda **kwargs: DeploymentMetadata(
            "N", "DISPOSABLE", {"DBPM_ALLOW_ENVIRONMENT_RESET": "Y"},
        ),
    )
    monkeypatch.setattr(
        cli, "get_application_state",
        lambda **kwargs: ApplicationState("CORE", "3.5.0", "C", "abc"),
    )
    monkeypatch.setattr(
        cli, "get_installed_application_graph",
        lambda **kwargs: (["CORE", "CONSUMER"], []),
    )
    monkeypatch.setattr(cli, "execute_plan", lambda plan, **kwargs: 0)

    assert cli.main([
        "dev", "reset-environment", "--keep", "CORE", "--yes",
        "--connect", "user/pass@db",
    ]) == 2
    assert "requires --confirm" in capsys.readouterr().err

    assert cli.main([
        "dev", "reset-environment", "--keep", "CORE", "--yes",
        "--confirm", "WRONG-SCHEMA", "--connect", "user/pass@db",
    ]) == 2
    assert "must match the target schema" in capsys.readouterr().err


def test_environment_reset_rejects_secret_and_config_purge_choices():
    parser = cli._build_parser()
    for category in ("secret", "config"):
        with pytest.raises(SystemExit):
            parser.parse_args([
                "dev", "reset-environment", "--keep", "CORE",
                "--connect", "user/pass@db", "--purge-var", category,
            ])


def test_environment_reset_classifies_preserved_state_and_purges_selected_category(
    monkeypatch, capsys, tmp_path: Path,
):
    from dbpm.lifecycle import write_lifecycle_receipt

    prefix = tmp_path / "consumer"
    (prefix / "var" / "cache").mkdir(parents=True)
    (prefix / "var" / "cache" / "a.tmp").write_text("x", encoding="utf-8")
    (prefix / "etc" / "unknown.conf").parent.mkdir(parents=True, exist_ok=True)
    (prefix / "etc" / "unknown.conf").write_text("x", encoding="utf-8")
    write_lifecycle_receipt(
        {
            "package": {
                "application_name": "CONSUMER",
                "state": [{"path": "var/cache/**", "category": "cache"}],
            },
            "application_runtime": {"root_package": "consumer", "payloads": []},
        },
        runtime_prefix=str(prefix),
    )

    monkeypatch.setattr(
        cli, "get_core_deployment_metadata",
        lambda **kwargs: DeploymentMetadata(
            "N", "DISPOSABLE", {"DBPM_ALLOW_ENVIRONMENT_RESET": "Y"},
        ),
    )
    monkeypatch.setattr(
        cli, "get_application_state",
        lambda **kwargs: ApplicationState("CORE", "3.5.0", "C", "abc"),
    )
    monkeypatch.setattr(
        cli, "get_installed_application_graph",
        lambda **kwargs: (["CORE", "CONSUMER"], []),
    )
    captured = {}
    monkeypatch.setattr(cli, "execute_plan", lambda plan, **kwargs: captured.setdefault("plan", plan) or 0)

    assert cli.main([
        "dev", "reset-environment", "--keep", "CORE", "--yes",
        "--confirm", "APP", "--connect", "user/pass@db",
        "--runtime-prefix", str(prefix), "--purge-var", "cache",
    ]) == 0

    plan = captured["plan"]
    assert plan["purge_categories"] == ["cache"]
    consumer_state = plan["preserved_state"]["CONSUMER"]
    assert consumer_state["categories"]["cache"] == ["var/cache/a.tmp"]
    assert consumer_state["unclassified"] == ["etc/unknown.conf"]


def test_environment_reset_warns_about_applications_missing_runtime_prefix(
    monkeypatch, capsys, tmp_path: Path,
):
    from dbpm.lifecycle import write_lifecycle_receipt

    prefix = tmp_path / "consumer"
    (prefix / "var").mkdir(parents=True)
    write_lifecycle_receipt(
        {
            "package": {"application_name": "CONSUMER", "state": []},
            "application_runtime": {"root_package": "consumer", "payloads": []},
        },
        runtime_prefix=str(prefix),
    )

    monkeypatch.setattr(
        cli, "get_core_deployment_metadata",
        lambda **kwargs: DeploymentMetadata(
            "N", "DISPOSABLE", {"DBPM_ALLOW_ENVIRONMENT_RESET": "Y"},
        ),
    )
    monkeypatch.setattr(
        cli, "get_application_state",
        lambda **kwargs: ApplicationState("CORE", "3.5.0", "C", "abc"),
    )
    monkeypatch.setattr(
        cli, "get_installed_application_graph",
        lambda **kwargs: (["CORE", "CONSUMER", "BASE"], [("CONSUMER", "BASE")]),
    )
    captured = {}
    monkeypatch.setattr(cli, "execute_plan", lambda plan, **kwargs: captured.setdefault("plan", plan) or 0)

    assert cli.main([
        "dev", "reset-environment", "--keep", "CORE", "--yes",
        "--confirm", "APP", "--connect", "user/pass@db",
        "--runtime-prefix", str(prefix),
    ]) == 0

    plan = captured["plan"]
    assert plan["unscoped_applications"] == ["BASE"]
    assert "no --runtime-prefix supplied for BASE" in capsys.readouterr().err


def test_environment_reset_aggregates_state_rules_from_multi_package_receipt(
    monkeypatch, capsys, tmp_path: Path,
):
    from dbpm.lifecycle import write_lifecycle_receipt

    prefix = tmp_path / "consumer"
    (prefix / "var" / "cache").mkdir(parents=True)
    (prefix / "var" / "cache" / "a.tmp").write_text("x", encoding="utf-8")
    write_lifecycle_receipt(
        {
            "package": {"application_name": "CONSUMER", "state": []},
            "packages": [
                {"package": {"application_name": "CONSUMER", "state": []}},
                {
                    "package": {
                        "application_name": "BASE",
                        "state": [{"path": "var/cache/**", "category": "cache"}],
                    }
                },
            ],
            "application_runtime": {"root_package": "consumer", "payloads": []},
        },
        runtime_prefix=str(prefix),
    )

    monkeypatch.setattr(
        cli, "get_core_deployment_metadata",
        lambda **kwargs: DeploymentMetadata(
            "N", "DISPOSABLE", {"DBPM_ALLOW_ENVIRONMENT_RESET": "Y"},
        ),
    )
    monkeypatch.setattr(
        cli, "get_application_state",
        lambda **kwargs: ApplicationState("CORE", "3.5.0", "C", "abc"),
    )
    monkeypatch.setattr(
        cli, "get_installed_application_graph",
        lambda **kwargs: (["CORE", "CONSUMER"], []),
    )
    captured = {}
    monkeypatch.setattr(cli, "execute_plan", lambda plan, **kwargs: captured.setdefault("plan", plan) or 0)

    assert cli.main([
        "dev", "reset-environment", "--keep", "CORE", "--yes",
        "--confirm", "APP", "--connect", "user/pass@db",
        "--runtime-prefix", str(prefix),
    ]) == 0

    consumer_state = captured["plan"]["preserved_state"]["CONSUMER"]
    assert consumer_state["categories"]["cache"] == ["var/cache/a.tmp"]


def _lifecycle_package_plan(app_name: str, *, reason: str) -> dict[str, object]:
    return {
        "package": {"name": app_name.lower(), "application_name": app_name, "version": "1.0.0"},
        "installation_reason": reason,
        "lifecycle": {
            "uninstall": {"path": f"{app_name}/uninstall.sql", "ref": f"/store/{app_name}/uninstall.sql"}
        },
    }


def _lifecycle_plan(
    entries: list[dict[str, object]],
    *,
    root_app: str,
    application_runtime: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "dbpm.multi-plan.v0",
        "mode": "install",
        "package": {"name": root_app.lower(), "application_name": root_app, "version": "1.0.0"},
        "packages": entries,
        "application_runtime": application_runtime,
    }


def _uninstall_args(application: str, runtime_prefix: Path, *extra: str):
    return cli._build_parser().parse_args(
        [
            "uninstall",
            "--application",
            application,
            "--runtime-prefix",
            str(runtime_prefix),
            "--cascade",
            "unused",
            "--allow-destructive",
            *extra,
        ]
    )


def test_uninstall_cascade_scopes_allow_destructive_to_root_package(tmp_path: Path, monkeypatch):
    receipt = _lifecycle_plan(
        [
            _lifecycle_package_plan("DEMO", reason="AUTO_DEPENDENCY"),
            _lifecycle_package_plan("CONSUMER", reason="APPLICATION_ROOT"),
        ],
        root_app="CONSUMER",
    )
    monkeypatch.setattr(cli, "load_lifecycle_receipt", lambda **kwargs: receipt)
    monkeypatch.setattr(cli, "_get_installed_state", lambda args, app: None)
    monkeypatch.setattr(cli, "_get_reverse_dependencies", lambda args, app: [])

    args = _uninstall_args("CONSUMER", tmp_path)
    plan = cli._build_installed_uninstall_plan(args)

    by_app = {item["package"]["application_name"]: item for item in plan["packages"]}
    assert by_app.keys() == {"CONSUMER", "DEMO"}
    assert by_app["CONSUMER"]["policy"]["result"] == "allowed"
    assert by_app["DEMO"]["policy"]["result"] == "requires-approval"
    assert "`uninstall` requires --allow-destructive" in by_app["DEMO"]["policy"]["required_approvals"]


def test_uninstall_cascade_keeps_dependency_still_needed_by_kept_sibling(tmp_path: Path, monkeypatch):
    # ROOT depends on PKG_B, which depends on PKG_A. PKG_B has an external
    # dependent (OTHER_APP) so it must survive cascade removal; PKG_A's only
    # dependent within this receipt is PKG_B, which is being kept, so PKG_A
    # must also survive even though nothing outside this receipt needs it.
    receipt = _lifecycle_plan(
        [
            _lifecycle_package_plan("PKG_A", reason="AUTO_DEPENDENCY"),
            _lifecycle_package_plan("PKG_B", reason="AUTO_DEPENDENCY"),
            _lifecycle_package_plan("ROOT", reason="APPLICATION_ROOT"),
        ],
        root_app="ROOT",
    )
    monkeypatch.setattr(cli, "load_lifecycle_receipt", lambda **kwargs: receipt)
    monkeypatch.setattr(cli, "_get_installed_state", lambda args, app: None)

    def fake_reverse_dependencies(args, app_name):
        return {
            "PKG_B": ["ROOT", "OTHER_APP"],
            "PKG_A": ["PKG_B"],
        }.get(app_name, [])

    monkeypatch.setattr(cli, "_get_reverse_dependencies", fake_reverse_dependencies)

    args = _uninstall_args("ROOT", tmp_path)
    plan = cli._build_installed_uninstall_plan(args)

    removed = {item["package"]["application_name"] for item in plan["packages"]}
    assert removed == {"ROOT"}


def test_uninstall_partial_cascade_skips_application_runtime_teardown(
    tmp_path: Path, monkeypatch, capsys
):
    runtime_graph = {"receipt_backed": True, "payloads": [], "commands": [], "effects": {}}
    receipt = _lifecycle_plan(
        [
            _lifecycle_package_plan("PKG_A", reason="AUTO_DEPENDENCY"),
            _lifecycle_package_plan("ROOT", reason="APPLICATION_ROOT"),
        ],
        root_app="ROOT",
        application_runtime=runtime_graph,
    )
    monkeypatch.setattr(cli, "load_lifecycle_receipt", lambda **kwargs: receipt)
    monkeypatch.setattr(cli, "_get_installed_state", lambda args, app: None)
    monkeypatch.setattr(
        cli, "_get_reverse_dependencies", lambda args, app: ["EXTERNAL_APP"] if app == "PKG_A" else []
    )

    args = _uninstall_args("ROOT", tmp_path)
    plan = cli._build_installed_uninstall_plan(args)

    assert {item["package"]["application_name"] for item in plan["packages"]} == {"ROOT"}
    assert plan["application_runtime"] is None
    assert "Skipping application runtime teardown" in capsys.readouterr().err


def test_uninstall_full_cascade_retains_application_runtime_for_teardown(
    tmp_path: Path, monkeypatch
):
    runtime_graph = {"receipt_backed": True, "payloads": [], "commands": [], "effects": {}}
    receipt = _lifecycle_plan(
        [
            _lifecycle_package_plan("PKG_A", reason="AUTO_DEPENDENCY"),
            _lifecycle_package_plan("ROOT", reason="APPLICATION_ROOT"),
        ],
        root_app="ROOT",
        application_runtime=runtime_graph,
    )
    monkeypatch.setattr(cli, "load_lifecycle_receipt", lambda **kwargs: receipt)
    monkeypatch.setattr(cli, "_get_installed_state", lambda args, app: None)
    monkeypatch.setattr(cli, "_get_reverse_dependencies", lambda args, app: [])

    args = _uninstall_args("ROOT", tmp_path)
    plan = cli._build_installed_uninstall_plan(args)

    assert {item["package"]["application_name"] for item in plan["packages"]} == {"ROOT", "PKG_A"}
    assert plan["application_runtime"] is not None
    assert plan["application_runtime"]["effects"]["operation"] == "uninstall"


def _skip_runtime_args(*extra: str) -> object:
    return cli._build_parser().parse_args(["install", "--skip-runtime", *extra])


def test_apply_skip_runtime_is_noop_when_flag_not_set():
    plan = {"application_runtime": {"receipt_backed": True}, "policy": {"policy_context": {"deployment_locked": False}}}
    args = cli._build_parser().parse_args(["install"])

    cli._apply_skip_runtime(plan, args)

    assert plan["application_runtime"] == {"receipt_backed": True}


def test_apply_skip_runtime_is_noop_when_plan_has_no_runtime():
    plan = {"application_runtime": None, "policy": {"policy_context": {"deployment_locked": True}}}
    args = _skip_runtime_args()

    cli._apply_skip_runtime(plan, args)

    assert plan["application_runtime"] is None


def test_apply_skip_runtime_nulls_runtime_when_unlocked(capsys):
    plan = {
        "application_runtime": {"receipt_backed": True},
        "policy": {"policy_context": {"deployment_locked": False}},
    }
    args = _skip_runtime_args()

    cli._apply_skip_runtime(plan, args)

    assert plan["application_runtime"] is None
    assert "Skipping application runtime staging/activation" in capsys.readouterr().err


def test_apply_skip_runtime_reads_policy_from_first_child_for_multi_package_plan():
    plan = {
        "application_runtime": {"receipt_backed": True},
        "packages": [
            {"policy": {"policy_context": {"deployment_locked": False}}},
            {"policy": {"policy_context": {"deployment_locked": True}}},
        ],
    }
    args = _skip_runtime_args()

    cli._apply_skip_runtime(plan, args)

    assert plan["application_runtime"] is None


def test_apply_skip_runtime_blocked_when_core_deploy_locked():
    plan = {
        "application_runtime": {"receipt_backed": True},
        "policy": {"policy_context": {"deployment_locked": True}},
    }
    args = _skip_runtime_args()

    with pytest.raises(cli.DbpmError, match="--skip-runtime requires DEPLOY_LOCKED=N"):
        cli._apply_skip_runtime(plan, args)

    assert plan["application_runtime"] == {"receipt_backed": True}


def test_install_dry_run_with_skip_runtime_nulls_plan_runtime(tmp_path: Path, monkeypatch, capsys):
    package = tmp_path / "package"
    _write_package(package)

    real_build_plan = cli._build_plan

    def fake_build_plan(command, args, **kwargs):
        plan = real_build_plan(command, args, **kwargs)
        plan["application_runtime"] = {"receipt_backed": True}
        plan["policy"] = {"policy_context": {"deployment_locked": False}}
        return plan

    monkeypatch.setattr(cli, "_build_plan", fake_build_plan)

    assert cli.main(["install", str(package), "--skip-runtime", "--dry-run"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["application_runtime"] is None


def test_install_with_skip_runtime_blocked_when_core_deploy_locked(tmp_path: Path, monkeypatch, capsys):
    package = tmp_path / "package"
    _write_package(package)

    real_build_plan = cli._build_plan

    def fake_build_plan(command, args, **kwargs):
        plan = real_build_plan(command, args, **kwargs)
        plan["application_runtime"] = {"receipt_backed": True}
        plan["policy"] = {"policy_context": {"deployment_locked": True}}
        return plan

    monkeypatch.setattr(cli, "_build_plan", fake_build_plan)

    assert cli.main(["install", str(package), "--skip-runtime", "--dry-run"]) == 2

    assert "--skip-runtime requires DEPLOY_LOCKED=N" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["1", "true", "True", "yes", "on"])
def test_skip_runtime_flag_defaults_on_when_env_var_truthy(monkeypatch, value):
    monkeypatch.setenv("DBPM_SKIP_RUNTIME", value)

    args = cli._build_parser().parse_args(["install"])

    assert args.skip_runtime is True


@pytest.mark.parametrize("value", ["0", "false", "", "no"])
def test_skip_runtime_flag_defaults_off_when_env_var_not_truthy(monkeypatch, value):
    monkeypatch.setenv("DBPM_SKIP_RUNTIME", value)

    args = cli._build_parser().parse_args(["install"])

    assert args.skip_runtime is False


def test_skip_runtime_flag_defaults_off_when_env_var_unset(monkeypatch):
    monkeypatch.delenv("DBPM_SKIP_RUNTIME", raising=False)

    args = cli._build_parser().parse_args(["install"])

    assert args.skip_runtime is False


def test_install_with_skip_runtime_env_var_nulls_plan_runtime(
    tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.setenv("DBPM_SKIP_RUNTIME", "1")
    package = tmp_path / "package"
    _write_package(package)

    real_build_plan = cli._build_plan

    def fake_build_plan(command, args, **kwargs):
        plan = real_build_plan(command, args, **kwargs)
        plan["application_runtime"] = {"receipt_backed": True}
        plan["policy"] = {"policy_context": {"deployment_locked": False}}
        return plan

    monkeypatch.setattr(cli, "_build_plan", fake_build_plan)

    assert cli.main(["install", str(package), "--dry-run"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["application_runtime"] is None


def test_uninstall_runtime_less_application_does_not_require_runtime_prefix(
    tmp_path: Path, monkeypatch
):
    receipt = _lifecycle_plan(
        [_lifecycle_package_plan("ROOT", reason="APPLICATION_ROOT")],
        root_app="ROOT",
        application_runtime=None,
    )
    monkeypatch.setattr(cli, "load_lifecycle_receipt", lambda **kwargs: receipt)
    monkeypatch.setattr(cli, "_get_installed_state", lambda args, app: None)
    monkeypatch.setattr(cli, "_get_reverse_dependencies", lambda args, app: [])

    args = cli._build_parser().parse_args(
        [
            "uninstall",
            "--application",
            "ROOT",
            "--cascade",
            "unused",
            "--allow-destructive",
        ]
    )
    plan = cli._build_installed_uninstall_plan(args)

    assert {item["package"]["application_name"] for item in plan["packages"]} == {"ROOT"}
    assert plan.get("application_runtime") is None


def _resume_lifecycle_package_plan(app_name: str, *, reason: str) -> dict[str, object]:
    return {
        "package": {"name": app_name.lower(), "application_name": app_name, "version": "1.0.0"},
        "installation_reason": reason,
        "lifecycle": {
            "install": {"path": f"{app_name}/install.sql", "ref": f"/store/{app_name}/install.sql"}
        },
    }


def test_resume_recovers_multi_package_plan_when_root_never_started(tmp_path: Path, monkeypatch):
    # DEP_A completed, DEP_B is mid-deploy (failed), and ROOT -- deployed
    # last -- never got a row in APPLICATION at all. Resuming should not be
    # rejected outright just because ROOT's state is None; ROOT has simply
    # not been attempted yet.
    receipt = _lifecycle_plan(
        [
            _resume_lifecycle_package_plan("DEP_A", reason="AUTO_DEPENDENCY"),
            _resume_lifecycle_package_plan("DEP_B", reason="AUTO_DEPENDENCY"),
            _resume_lifecycle_package_plan("ROOT", reason="APPLICATION_ROOT"),
        ],
        root_app="ROOT",
    )
    monkeypatch.setattr(cli, "load_lifecycle_receipt", lambda **kwargs: receipt)
    monkeypatch.setattr(
        cli,
        "get_current_operation",
        lambda **kwargs: OperationRecord(
            operation_id="op-1",
            application_name="ROOT",
            mode="install",
            state="RUNNING",
            attempt_number=1,
            lease_token=None,
            lease_expiry=None,
        ),
    )
    states = {
        "DEP_A": {"application_name": "DEP_A", "version": "1.0.0", "deploy_status": "C"},
        "DEP_B": {"application_name": "DEP_B", "version": "1.0.0", "deploy_status": "R"},
        "ROOT": None,
    }
    monkeypatch.setattr(cli, "_get_installed_state", lambda args, app: states.get(app))

    args = cli._build_parser().parse_args(
        [
            "resume", "--application", "ROOT", "--runtime-prefix", str(tmp_path),
            "--connect", "user/pass@db",
        ]
    )
    plan = cli._build_installed_resume_plan(args)

    by_app = {item["package"]["application_name"]: item for item in plan["packages"]}
    assert by_app["DEP_A"]["mode"] == "resume"
    assert by_app["DEP_B"]["mode"] == "resume"
    assert by_app["ROOT"]["mode"] == "install"
    assert by_app["ROOT"]["installed_state"] is None

    for item in plan["packages"]:
        cli._enforce_installed_state(item)


def _resume_lifecycle_package_plan_with_upgrade(app_name: str, *, reason: str) -> dict[str, object]:
    return {
        "package": {"name": app_name.lower(), "application_name": app_name, "version": "1.0.0"},
        "installation_reason": reason,
        "lifecycle": {
            "install": {"path": f"{app_name}/install.sql", "ref": f"/store/{app_name}/install.sql"},
            "upgrade": {"path": f"{app_name}/upgrade.sql", "ref": f"/store/{app_name}/upgrade.sql"},
        },
    }


def test_resume_reruns_upgrade_script_for_stuck_upgrade_operation(tmp_path: Path, monkeypatch):
    # The operation in flight was an `upgrade`, not an `install`. Resuming it
    # should re-run the upgrade script for a package already underway, since
    # an install script can legitimately refuse to run against a schema its
    # own upgrade already touched.
    receipt = _lifecycle_plan(
        [_resume_lifecycle_package_plan_with_upgrade("ROOT", reason="APPLICATION_ROOT")],
        root_app="ROOT",
    )
    monkeypatch.setattr(cli, "load_lifecycle_receipt", lambda **kwargs: receipt)
    monkeypatch.setattr(
        cli,
        "get_current_operation",
        lambda **kwargs: OperationRecord(
            operation_id="op-1",
            application_name="ROOT",
            mode="upgrade",
            state="RUNNING",
            attempt_number=1,
            lease_token=None,
            lease_expiry=None,
        ),
    )
    states = {
        "ROOT": {"application_name": "ROOT", "version": "1.0.0", "deploy_status": "R"},
    }
    monkeypatch.setattr(cli, "_get_installed_state", lambda args, app: states.get(app))

    args = cli._build_parser().parse_args(
        [
            "resume", "--application", "ROOT", "--runtime-prefix", str(tmp_path),
            "--connect", "user/pass@db",
        ]
    )
    plan = cli._build_installed_resume_plan(args)

    by_app = {item["package"]["application_name"]: item for item in plan["packages"]}
    assert by_app["ROOT"]["mode"] == "resume"
    assert by_app["ROOT"]["execution"]["script"] == "ROOT/upgrade.sql"
    assert by_app["ROOT"]["execution"]["script_ref"] == "/store/ROOT/upgrade.sql"


def test_resume_falls_back_to_install_script_for_package_not_yet_started(
    tmp_path: Path, monkeypatch
):
    # Even when the in-flight operation's mode is "upgrade", a package that
    # hasn't been reached yet (no APPLICATION row) has nothing to resume and
    # should still be treated as a fresh install.
    receipt = _lifecycle_plan(
        [_resume_lifecycle_package_plan_with_upgrade("ROOT", reason="APPLICATION_ROOT")],
        root_app="ROOT",
    )
    monkeypatch.setattr(cli, "load_lifecycle_receipt", lambda **kwargs: receipt)
    monkeypatch.setattr(
        cli,
        "get_current_operation",
        lambda **kwargs: OperationRecord(
            operation_id="op-1",
            application_name="ROOT",
            mode="upgrade",
            state="RUNNING",
            attempt_number=1,
            lease_token=None,
            lease_expiry=None,
        ),
    )
    monkeypatch.setattr(cli, "_get_installed_state", lambda args, app: None)

    args = cli._build_parser().parse_args(
        [
            "resume", "--application", "ROOT", "--runtime-prefix", str(tmp_path),
            "--connect", "user/pass@db",
        ]
    )
    plan = cli._build_installed_resume_plan(args)

    by_app = {item["package"]["application_name"]: item for item in plan["packages"]}
    assert by_app["ROOT"]["mode"] == "install"
    assert by_app["ROOT"]["execution"]["script"] == "ROOT/install.sql"


def test_resume_as_overrides_target_package_script_in_composite_plan(
    tmp_path: Path, monkeypatch
):
    # --as lets the user override script selection for the package they
    # targeted, even when automatic detection (operation.mode) would have
    # picked something else.
    receipt = _lifecycle_plan(
        [_resume_lifecycle_package_plan_with_upgrade("ROOT", reason="APPLICATION_ROOT")],
        root_app="ROOT",
    )
    monkeypatch.setattr(cli, "load_lifecycle_receipt", lambda **kwargs: receipt)
    monkeypatch.setattr(
        cli,
        "get_current_operation",
        lambda **kwargs: OperationRecord(
            operation_id="op-1",
            application_name="ROOT",
            mode="install",
            state="RUNNING",
            attempt_number=1,
            lease_token=None,
            lease_expiry=None,
        ),
    )
    states = {
        "ROOT": {"application_name": "ROOT", "version": "1.0.0", "deploy_status": "R"},
    }
    monkeypatch.setattr(cli, "_get_installed_state", lambda args, app: states.get(app))

    args = cli._build_parser().parse_args(
        [
            "resume", "--application", "ROOT", "--as", "upgrade",
            "--runtime-prefix", str(tmp_path), "--connect", "user/pass@db",
        ]
    )
    plan = cli._build_installed_resume_plan(args)

    by_app = {item["package"]["application_name"]: item for item in plan["packages"]}
    assert by_app["ROOT"]["execution"]["script"] == "ROOT/upgrade.sql"


def test_uninstall_runtime_bearing_application_still_requires_runtime_prefix(
    tmp_path: Path, monkeypatch
):
    runtime_graph = {"receipt_backed": True, "payloads": [], "commands": [], "effects": {}}
    receipt = _lifecycle_plan(
        [_lifecycle_package_plan("ROOT", reason="APPLICATION_ROOT")],
        root_app="ROOT",
        application_runtime=runtime_graph,
    )
    monkeypatch.setattr(cli, "load_lifecycle_receipt", lambda **kwargs: receipt)
    monkeypatch.setattr(cli, "_get_installed_state", lambda args, app: None)
    monkeypatch.setattr(cli, "_get_reverse_dependencies", lambda args, app: [])

    args = cli._build_parser().parse_args(
        [
            "uninstall",
            "--application",
            "ROOT",
            "--cascade",
            "unused",
            "--allow-destructive",
        ]
    )

    with pytest.raises(cli.DbpmError, match="Application runtime requires --runtime-prefix"):
        cli._build_installed_uninstall_plan(args)
