from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import sys
import uuid
from pathlib import Path

from .chain import ChainError, resolve_upgrade_chain
from .connect import ConnectSpec, connect_string, sqlcl_name, validate_connect_spec
from .resolver import parse_version
from .publisher import (
    PUBLISH_RECEIPT_NAME,
    PublishReceipt,
    build_artifact,
    create_publish_receipt,
    publish_to_repository,
    resolve_signing_key_fingerprint,
    verify_publish,
    write_publish_receipt,
)
from .registry import (
    create_registry_index_payload,
    index_registry_version,
    load_publish_receipt,
    registry_base_url,
)
from .db import (
    TargetIdentity,
    check_core,
    get_application_state,
    get_core_deployment_metadata,
    get_deployment_provenance,
    get_current_operation,
    get_installed_application_graph,
    get_target_identity,
    get_reverse_dependencies,
)
from .environment import DeploymentPolicy, policy_from_core_values, resolve_deployment_policy
from .errors import DbpmError
from .executor import execute_plan
from .application_runtime import (
    classify_preserved_state,
    load_retained_application_runtime_receipt,
    rollback_application_runtime,
    validate_application_runtime_collisions,
)
from .manifest import NEVER_PURGED_STATE_CATEGORIES, STATE_CATEGORIES
from .lockfile import (
    LOCKFILE_NAME,
    assert_database_matches_lockfile,
    assert_database_provenance_matches_lockfile,
    assert_database_states_match_lockfile,
    assert_lockfile_matches_plan,
    create_lockfile,
    deployment_provenance_requests,
    load_lockfile,
    lockfile_package_sources_with_checksums,
    write_lockfile,
)
from .planner import create_plan
from .provenance import resolve_provenance
from .resolver import create_multi_package_plan
from .script_generator import generate_scripts, resolve_generation_options
from .source import load_package_source
from .workspace import (
    is_workspace_root,
    load_workspace,
    select_workspace_package,
    workspace_dependency_sources,
)
from .initializer import init_package, init_workspace, validate_package_name
from .progress import report_progress
from .lifecycle import (
    lifecycle_receipt_path,
    load_lifecycle_receipt,
    snapshot_plan,
    write_lifecycle_receipt,
)


CONNECT_OPTIONS_CONFLICT_MESSAGE = (
    "Database connection inputs are mutually exclusive. Use --connect/DBPM_CONNECT for a raw "
    "Oracle connect string, DBPM_DB_USER/DBPM_DB_PASSWORD/DBPM_DB_DSN for structured database "
    "credentials, or --connect-name/DBPM_CONNECT_NAME for a SQLcl saved connection."
)

DB_CONNECT_ENV_NAMES = ("DBPM_DB_USER", "DBPM_DB_PASSWORD", "DBPM_DB_DSN")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            _run_init(args)
            return 0
        if args.command == "publish":
            _run_publish(args)
            return 0
        if args.command == "registry":
            _run_registry(args)
            return 0
        if args.command == "workspace":
            _run_workspace(args)
            return 0
        if args.command == "generate-scripts":
            _run_generate_scripts(args)
            return 0
        if args.command == "runtime":
            _run_runtime(args)
            return 0
        if args.command == "dev":
            if args.dev_command == "reset":
                args.allow_destructive = True
                plan = _build_plan(
                    "reinstall", args, include_installed_state=True,
                    show_progress=not args.dry_run and getattr(args, "verbose", False),
                )
                _attach_target_identity(plan, args)
                _attach_plan_identity(plan, "dev reset")
                if args.dry_run:
                    _enrich_destructive_preview(plan, args)
                    _print_json(plan)
                    return 0
                _enforce_plan_policies(plan)
                _confirm_destructive_plan(plan, args)
                _execute_or_explain(plan, args)
                _report_execution_success("dev reset", plan)
                return 0
            if args.dev_command == "reset-environment":
                plan = _build_environment_reset_plan(args)
                if args.dry_run:
                    _print_json(plan)
                    return 0
                _execute_or_explain_policy(plan)
                _confirm_destructive_plan(plan, args)
                execute_plan(plan, connect=_connect_spec(args), runner=args.runner)
                report_progress("Development environment reset completed successfully")
                return 0
        if args.command == "plan":
            plan = _build_plan(args.mode, args, include_installed_state=_has_database_access(args))
            _print_json(plan)
            return 0
        if args.command == "lock":
            if args.check_db and not args.check:
                raise DbpmError("--check-db requires --check")
            plan = _build_plan("install", args, include_installed_state=False)
            lockfile_path = Path(args.output)
            if args.check:
                lockfile = load_lockfile(lockfile_path)
                assert_lockfile_matches_plan(lockfile, plan)
                if args.check_db:
                    if not _has_database_access(args):
                        raise DbpmError(
                            "Database lockfile check requires --connect/DBPM_CONNECT, "
                            "DBPM_DB_USER/DBPM_DB_PASSWORD/DBPM_DB_DSN, or "
                            "--connect-name/DBPM_CONNECT_NAME"
                        )
                    states = {
                        app_name: _get_installed_state(args, app_name)
                        for app_name, _ in deployment_provenance_requests(lockfile)
                    }
                    assert_database_states_match_lockfile(lockfile, states)
                    provenances = {
                        app_name: get_deployment_provenance(
                            connect=_connect_spec(args),
                            runner=args.runner,
                            application_name=app_name,
                            version=version,
                        )
                        for app_name, version in deployment_provenance_requests(lockfile)
                    }
                    assert_database_provenance_matches_lockfile(lockfile, provenances)
                print(f"LOCKFILE_OK={lockfile_path}")
                return 0
            lockfile = create_lockfile(plan)
            write_lockfile(lockfile, lockfile_path)
            print(f"WROTE_LOCKFILE={lockfile_path}")
            return 0
        if args.command == "check-core":
            result = check_core(
                connect=_connect_spec(args),
                runner=args.runner,
                minimum_version=args.minimum_version,
            )
            print(result.stdout.strip())
            return 0
        if args.command == "rollback":
            prefix = Path(args.runtime_prefix).expanduser().resolve()
            target = load_retained_application_runtime_receipt(
                prefix,
                target_generation=args.target_generation,
            )
            database_versions: dict[str, str] = {}
            for package in target.packages:
                state = _get_installed_state(args, _application_name(package.name))
                if isinstance(state, dict) and isinstance(state.get("version"), str):
                    database_versions[package.name] = state["version"]
            receipt = rollback_application_runtime(
                prefix,
                database_versions=database_versions,
                target_generation=args.target_generation,
            )
            print(f"ROLLED_BACK_RUNTIME_GENERATION={receipt.generation}")
            report_progress(
                f"Runtime rollback completed successfully: generation {receipt.generation}"
            )
            return 0
        if args.command in {"bootstrap-core", "install", "upgrade", "reinstall", "resume", "validate", "uninstall"}:
            if not args.dry_run:
                report_progress(f"Preparing {args.command} plan...")
            if args.command == "install" and args.lockfile:
                plan = _build_plan_from_lockfile(
                    args,
                    include_installed_state=not args.dry_run,
                    show_progress=not args.dry_run and getattr(args, "verbose", False),
                )
            elif args.command == "uninstall" and args.source is None:
                plan = _build_installed_uninstall_plan(args)
            elif args.command == "resume" and args.runtime_prefix and (
                args.source is None
                or lifecycle_receipt_path(args.runtime_prefix).is_file()
            ):
                plan = _build_installed_resume_plan(args)
            else:
                if args.command == "install" and args.source is None and not getattr(args, "package", None):
                    raise DbpmError("install requires a source or --lockfile")
                include_installed = not args.dry_run or (
                    args.command in {"upgrade", "reinstall"} and _has_database_access(args)
                )
                plan = _build_plan(
                    args.command,
                    args,
                    include_installed_state=include_installed,
                    show_progress=not args.dry_run and getattr(args, "verbose", False),
                )
            if args.command == "reinstall":
                if _has_database_access(args):
                    _attach_target_identity(plan, args)
                _attach_plan_identity(plan, "reinstall")
            if args.dry_run:
                if args.command == "reinstall":
                    _enrich_destructive_preview(plan, args)
                _print_json(plan)
                return 0
            if args.command == "reinstall" and getattr(args, "cascade", None) == "graph":
                _confirm_destructive_plan(plan, args)
            _execute_or_explain(plan, args)
            _report_execution_success(args.command, plan)
            return 0
    except DbpmError as exc:
        print(f"dbpm: {exc}", file=sys.stderr)
        return 2

    parser.error("No command selected")
    return 2


def _run_publish(args: argparse.Namespace) -> None:
    from pathlib import Path
    from .errors import PublishError
    from .manifest import PublishConfig

    source_arg, _, _ = _resolve_workspace_source_arg(args.source, args)
    source = load_package_source(source_arg)
    manifest = source.manifest

    publish_config = manifest.publish
    if args.group or args.artifact_id:
        group = args.group or (publish_config.group if publish_config else None)
        if not group:
            raise DbpmError("--group is required when publish.group is not set in the manifest")
        artifact_id = args.artifact_id or (publish_config.artifact_id if publish_config else None)
        publish_config = PublishConfig(group=group, artifact_id=artifact_id)
    elif publish_config is None:
        raise DbpmError(
            "No publish configuration found. Add a publish: section to dbpm.yaml or use --group"
        )

    if not args.signing_key:
        raise DbpmError(
            "A signing key is required. Use --signing-key or set DBPM_SIGNING_KEY"
        )

    if args.dry_run:
        artifact_id = publish_config.artifact_id or manifest.name
        version = manifest.version
        artifact_name = f"{artifact_id}-{version}.zip"
        pom_name = f"{artifact_id}-{version}.pom"
        print(f"DRY_RUN: would publish {artifact_name} to {args.target}")
        print(f"  artifact: {artifact_name}")
        print(f"  pom:      {pom_name}")
        print(f"  checksums: {artifact_name}.sha256, {artifact_name}.sha1")
        print(f"  signature: {artifact_name}.asc")
        print(f"  group:     {publish_config.group}")
        print(f"  artifact_id: {artifact_id}")
        print(f"  version:   {version}")
        print(f"  signing_key: {args.signing_key}")
        return

    fingerprint = resolve_signing_key_fingerprint(args.signing_key)
    source_path = source.path
    artifact_path = build_artifact(source_path, manifest, publish_config)
    receipt = publish_to_repository(args.target, manifest, publish_config, artifact_path, args.signing_key)
    verify_publish(args.target, manifest, publish_config, manifest.version, receipt.checksum)
    publish_receipt = create_publish_receipt(
        manifest=manifest,
        publish_config=publish_config,
        target=args.target,
        receipt=receipt,
        publisher_key_fingerprint=fingerprint,
    )
    receipt_path = _publish_receipt_path(args.receipt_output, source_arg, source_path)
    write_publish_receipt(publish_receipt, receipt_path)
    print(f"PUBLISHED={receipt.artifact_url}")
    print(f"WROTE_PUBLISH_RECEIPT={receipt_path}")

    if args.index_registry is not None:
        try:
            payload = create_registry_index_payload(manifest, receipt=publish_receipt)
            token = _registry_token("DBPM_REGISTRY_TOKEN")
            result = index_registry_version(
                manifest.name,
                payload,
                registry_url=args.index_registry or None,
                token=token,
            )
        except DbpmError as exc:
            raise DbpmError(
                f"Publishing succeeded and receipt was written to {receipt_path}, "
                f"but registry indexing failed: {exc}"
            ) from exc
        print(f"INDEXED={result.get('package', manifest.name)}@{result.get('version', manifest.version)}")


def _run_registry(args: argparse.Namespace) -> None:
    if args.registry_command != "index":
        raise DbpmError("Unknown registry command")

    source_arg, _, _ = _resolve_workspace_source_arg(args.package_root, args)
    source = load_package_source(source_arg)
    package_root = source.path
    receipt_path = Path(args.receipt) if args.receipt else package_root / PUBLISH_RECEIPT_NAME
    receipt = (
        load_publish_receipt(receipt_path)
        if args.receipt or receipt_path.exists()
        else None
    )
    payload = create_registry_index_payload(
        source.manifest,
        receipt=receipt,
        publisher=args.publisher,
        description=args.description,
        artifact_url=args.artifact_url,
        artifact_checksum=args.artifact_checksum,
        artifact_signature_url=args.artifact_signature_url,
        publisher_key_fingerprint=args.publisher_key_fingerprint,
    )
    destination = (
        f"{registry_base_url(args.registry_url)}/packages/{source.manifest.name}/versions/index"
    )
    if args.dry_run:
        _print_json({"destination": destination, "payload": payload})
        return

    token = _registry_token(args.token_env)
    result = index_registry_version(
        source.manifest.name,
        payload,
        registry_url=args.registry_url,
        token=token,
    )
    print(f"INDEXED={result.get('package', source.manifest.name)}@{result.get('version', source.manifest.version)}")


def _run_runtime(args: argparse.Namespace) -> None:
    if args.runtime_command != "reconcile":
        raise DbpmError("Unknown runtime command")
    if args.replace:
        environment = _resolve_policy_for_plan("resume", args)
        environment.require("DBPM_ALLOW_RUNTIME_REPLACE")
    # Reconciliation is receipt-backed structural repair, not a policy escape
    # hatch: it must respect the same DEPLOY_LOCKED=Y/--approve evaluation
    # `_build_installed_resume_plan` already computed for `resume`. Nothing
    # here overrides that result.
    plan = _build_installed_resume_plan(args, allow_completed=True)
    if args.replace:
        plan["runtime_reconcile_replace"] = True
    if args.dry_run:
        graph = plan.get("application_runtime")
        if not isinstance(graph, dict):
            raise DbpmError("Installed lifecycle receipt has no application runtime graph")
        classifications = validate_application_runtime_collisions(
            graph, prefix=Path(args.runtime_prefix).expanduser().resolve(),
            mode="reinstall" if args.replace else "resume"
        )
        plan["runtime_reconciliation"] = {"classifications": list(classifications)}
        _print_json(plan)
        return
    _execute_or_explain(plan, args)
    report_progress(f"Runtime reconciliation completed successfully: {_package_progress_identity(plan)}")


def _publish_receipt_path(receipt_output: str | None, source_arg: str, source_path: Path) -> Path:
    if receipt_output:
        return Path(receipt_output)
    raw_source = Path(source_arg).expanduser()
    if raw_source.is_file():
        return Path.cwd() / PUBLISH_RECEIPT_NAME
    return source_path / PUBLISH_RECEIPT_NAME


def _registry_token(token_env: str) -> str:
    token = os.environ.get(token_env)
    if not token:
        raise DbpmError(f"Registry indexing requires token environment variable {token_env}")
    return token


def _run_init(args: argparse.Namespace) -> None:
    root = Path(args.directory).expanduser().resolve()
    if args.init_command == "package":
        name = args.name or (root.name if root.name not in {".", ""} else "my_package")
        validate_package_name(name)
        created = init_package(root, name=name, version=args.version,
                               description=args.description, force=args.force)
    elif args.init_command == "workspace":
        package_names = args.packages or ["my_package"]
        for pkg_name in package_names:
            validate_package_name(pkg_name)
        created = init_workspace(root, package_names=package_names, force=args.force)
    else:
        raise DbpmError("Unknown init command")
    for path in created:
        print(f"CREATED={path}")


def _run_workspace(args: argparse.Namespace) -> None:
    if args.workspace_command == "list":
        workspace = load_workspace(args.workspace)
        _print_json(workspace.as_dict())
        return
    raise DbpmError("Unknown workspace command")


def _run_generate_scripts(args: argparse.Namespace) -> None:
    options = resolve_generation_options(
        Path(args.source),
        from_ref=args.from_ref,
        to_ref=args.to_ref,
        version=args.target_version,
        application_name=args.application_name,
        install_output=args.install_output,
        release_upgrade_output=args.release_upgrade_output,
        upgrade_pointer_output=args.upgrade_pointer_output,
        deployment_type=args.deployment_type,
        check=args.check,
    )
    result = generate_scripts(options)
    for warning in result.warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    if args.check:
        print("GENERATED_SCRIPTS_OK")
        return
    for path in result.changed:
        print(f"WROTE={path.relative_to(options.root)}")


def _build_parser() -> argparse.ArgumentParser:
    from importlib.metadata import version as _pkg_version
    parser = argparse.ArgumentParser(prog="dbpm")
    parser.add_argument("--version", action="version", version=f"dbpm {_pkg_version('dbpm')}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check-core", help="Verify Core is available in a target database")
    _add_database_args(check)
    check.add_argument("--minimum-version", help="Minimum Core version, such as 3.0.0")

    plan = subparsers.add_parser("plan", help="Generate a deployment plan")
    _add_common_args(plan)
    plan.add_argument(
        "--mode",
        choices=("bootstrap-core", "install", "upgrade", "reinstall", "resume", "validate", "uninstall"),
        default="install",
        help="Deployment mode to plan",
    )
    _add_policy_arg(plan)
    _add_deploy_environment_arg(plan)
    _add_dependency_source_args(plan)
    _add_database_args(plan)

    lock = subparsers.add_parser("lock", help="Write or verify a dependency lockfile")
    _add_common_args(lock)
    _add_policy_arg(lock)
    _add_dependency_source_args(lock)
    _add_database_args(lock)
    lock.add_argument("--output", default=LOCKFILE_NAME, help=f"Lockfile path, default: {LOCKFILE_NAME}")
    lock.add_argument("--check", action="store_true", help="Verify the current resolution matches the lockfile")
    lock.add_argument(
        "--check-db",
        action="store_true",
        help="With --check, verify installed database versions match the lockfile",
    )

    bootstrap = subparsers.add_parser("bootstrap-core", help="Bootstrap Core")
    _add_common_args(bootstrap)
    _add_policy_arg(bootstrap)
    _add_deploy_environment_arg(bootstrap)
    _add_execution_args(bootstrap)

    install = subparsers.add_parser("install", help="Install a package")
    _add_common_args(install, source_required=False)
    _add_execution_args(install)
    _add_dependency_source_args(install)
    install.add_argument(
        "--lockfile",
        nargs="?",
        const=LOCKFILE_NAME,
        help=f"Install from a resolved lockfile, default when no value is provided: {LOCKFILE_NAME}",
    )

    upgrade = subparsers.add_parser("upgrade", help="Upgrade an installed package to a new version")
    _add_common_args(upgrade)
    _add_execution_args(upgrade)
    _add_dependency_source_args(upgrade)
    upgrade.add_argument(
        "--allow-dependent-break",
        action="store_true",
        help="Allow major upgrade even when installed dependents may have incompatible constraints",
    )

    reinstall = subparsers.add_parser("reinstall", help="Destructively reinstall a package")
    _add_common_args(reinstall)
    _add_execution_args(reinstall)
    reinstall.add_argument(
        "--allow-destructive",
        action="store_true",
        help="Allow destructive reinstall planning/execution",
    )
    _add_dependency_source_args(reinstall)
    reinstall.add_argument(
        "--cascade", choices=("graph",),
        help="Reinstall the complete source dependency graph; requires DBPM_ALLOW_GRAPH_RESET",
    )
    reinstall.add_argument("--yes", action="store_true", help="Skip interactive confirmation")
    reinstall.add_argument(
        "--confirm-delete-system",
        help="Required for Core reinstall; must be CORE",
    )

    resume = subparsers.add_parser("resume", help="Resume a running or failed deployment")
    _add_common_args(resume, source_required=False)
    _add_execution_args(resume)
    resume.add_argument("--application", help="Installed application operation to resume")

    validate = subparsers.add_parser("validate", help="Run a package validation script")
    _add_common_args(validate)
    _add_execution_args(validate)
    _add_dependency_source_args(validate)

    uninstall = subparsers.add_parser("uninstall", help="Uninstall an application package")
    _add_common_args(uninstall, source_required=False)
    _add_execution_args(uninstall)
    _add_dependency_source_args(uninstall)
    uninstall.add_argument(
        "--allow-destructive",
        action="store_true",
        help="Allow application uninstall planning/execution",
    )
    uninstall.add_argument(
        "--application",
        help="Installed application name (permits source-free uninstall with --runtime-prefix)",
    )
    uninstall.add_argument(
        "--cascade",
        choices=("unused", "graph"),
        help="Remove unused automatic dependencies; graph requires DBPM_ALLOW_GRAPH_RESET",
    )

    rollback = subparsers.add_parser(
        "rollback",
        help="Reactivate a retained application runtime generation",
    )
    rollback.add_argument("--runtime-prefix", required=True)
    rollback.add_argument("--target-generation", type=int)
    _add_database_args(rollback)

    runtime = subparsers.add_parser("runtime", help="Reconcile application runtime state")
    runtime_subparsers = runtime.add_subparsers(dest="runtime_command", required=True)
    reconcile = runtime_subparsers.add_parser(
        "reconcile", help="Restore runtime from the installed lifecycle receipt"
    )
    _add_common_args(reconcile, source_required=False)
    _add_execution_args(reconcile)
    reconcile.add_argument("--application", help="Installed application name")
    reconcile.add_argument(
        "--replace", action="store_true", help="Replace conflicting runtime destinations"
    )

    dev = subparsers.add_parser("dev", help="Policy-gated development lifecycle operations")
    dev_subparsers = dev.add_subparsers(dest="dev_command", required=True)
    dev_reset = dev_subparsers.add_parser(
        "reset", help="Replace a local package using canonical reinstall semantics"
    )
    _add_common_args(dev_reset)
    _add_execution_args(dev_reset)
    _add_dependency_source_args(dev_reset)
    dev_reset.add_argument("--cascade", choices=("graph",))
    dev_reset.add_argument("--yes", action="store_true", help="Skip interactive confirmation")

    reset_environment = dev_subparsers.add_parser(
        "reset-environment", help="Remove every non-CORE application"
    )
    _add_database_args(reset_environment)
    reset_environment.add_argument("--keep", required=True, choices=("CORE",))
    reset_environment.add_argument("--confirm", help="Expected schema or Core environment label")
    reset_environment.add_argument("--yes", action="store_true", help="Skip interactive confirmation")
    reset_environment.add_argument("--dry-run", action="store_true")
    reset_environment.add_argument(
        "--runtime-prefix", action="append", default=[],
        help="Installed application runtime prefix to remove (repeatable)",
    )
    reset_environment.add_argument(
        "--purge-var", action="append", default=[],
        choices=sorted(STATE_CATEGORIES - NEVER_PURGED_STATE_CATEGORIES),
        help=(
            "Manifest-classified var/etc category to delete instead of preserve "
            "(repeatable); config and secret can never be purged"
        ),
    )

    workspace = subparsers.add_parser("workspace", help="Inspect a dbpm workspace")
    workspace_subparsers = workspace.add_subparsers(dest="workspace_command", required=True)
    workspace_list = workspace_subparsers.add_parser("list", help="List packages in a dbpm workspace")
    workspace_list.add_argument(
        "workspace",
        nargs="?",
        default=".",
        help="Workspace root or dbpm-workspace.yaml path, default: current directory",
    )

    init = subparsers.add_parser("init", help="Scaffold a new package or workspace directory")
    init_subparsers = init.add_subparsers(dest="init_command", required=True)

    init_pkg = init_subparsers.add_parser("package", help="Initialize a dbpm package directory")
    init_pkg.add_argument("directory", nargs="?", default=".", help="Target directory, default: current directory")
    init_pkg.add_argument("--name", help="Package name (default: directory basename)")
    init_pkg.add_argument("--version", default="0.1.0", help="Initial version (default: 0.1.0)")
    init_pkg.add_argument("--description", default="", help="Package description")
    init_pkg.add_argument("--force", action="store_true", help="Allow init in a non-empty directory")

    init_ws = init_subparsers.add_parser("workspace", help="Initialize a dbpm workspace directory")
    init_ws.add_argument("directory", nargs="?", default=".", help="Target directory, default: current directory")
    init_ws.add_argument(
        "--package",
        action="append",
        dest="packages",
        metavar="NAME",
        help="Package name to scaffold under database/ (repeatable; default: my_package)",
    )
    init_ws.add_argument("--force", action="store_true", help="Allow init in a non-empty directory")

    generate = subparsers.add_parser(
        "generate-scripts",
        help="Generate standalone Oracle install and upgrade scripts from Git changes",
    )
    generate.add_argument("source", nargs="?", default=".", help="Git repository root")
    generate.add_argument("--from", dest="from_ref", help="Baseline Git commit or ref")
    generate.add_argument("--to", dest="to_ref", default="HEAD", help="Target Git commit or ref, default: HEAD")
    generate.add_argument("--version", dest="target_version", help="Target semantic version; overrides dbpm.yaml")
    generate.add_argument("--application-name", help="Application registry name; overrides dbpm.yaml")
    generate.add_argument(
        "--deployment-type",
        choices=("major", "minor", "patch"),
        help="Upgrade deployment type; normally inferred from the version delta",
    )
    generate.add_argument("--install-output", help="Generated full-install script path")
    generate.add_argument("--release-upgrade-output", help="Generated versioned upgrade script path")
    generate.add_argument("--upgrade-pointer-output", help="Generated current-upgrade pointer path")
    generate.add_argument("--check", action="store_true", help="Fail when generated scripts are stale or missing")

    publish = subparsers.add_parser("publish", help="Build and publish a package to a Maven repository")
    publish.add_argument("source", help="Local package directory or ZIP to publish")
    publish.add_argument(
        "--package",
        dest="package",
        help="Package name or application name to select when source is a workspace root",
    )
    publish.add_argument(
        "--target",
        required=True,
        help="Publish target: gh-maven:owner/repo or maven:https://...",
    )
    publish.add_argument(
        "--group",
        default=None,
        help="Maven group ID (overrides publish.group in manifest)",
    )
    publish.add_argument(
        "--artifact-id",
        default=None,
        dest="artifact_id",
        help="Maven artifact ID (overrides publish.artifact_id in manifest)",
    )
    publish.add_argument(
        "--signing-key",
        default=os.environ.get("DBPM_SIGNING_KEY"),
        dest="signing_key",
        help="GPG key ID, fingerprint, or email (default: DBPM_SIGNING_KEY)",
    )
    publish.add_argument(
        "--receipt-output",
        default=None,
        help=f"Publish receipt path, default: package root/{PUBLISH_RECEIPT_NAME}",
    )
    publish.add_argument(
        "--index-registry",
        nargs="?",
        const="",
        default=None,
        metavar="URL",
        help="Index the published artifact; optional URL defaults to DBPM_REGISTRY_URL or https://registry.dbpm.io",
    )
    publish.add_argument("--dry-run", action="store_true", help="Print what would be published without uploading")

    registry = subparsers.add_parser("registry", help="Interact with a dbpm registry")
    registry_subparsers = registry.add_subparsers(dest="registry_command", required=True)
    registry_index = registry_subparsers.add_parser("index", help="Index a published package artifact")
    registry_index.add_argument("package_root", nargs="?", default=".", help="Package or workspace root")
    registry_index.add_argument("--package", help="Package name or application name for a workspace root")
    registry_index.add_argument("--receipt", help=f"Publish receipt path, default: package root/{PUBLISH_RECEIPT_NAME}")
    registry_index.add_argument("--registry-url", default=None, help="Registry URL, default: DBPM_REGISTRY_URL or https://registry.dbpm.io")
    registry_index.add_argument("--token-env", default="DBPM_REGISTRY_TOKEN", help="Environment variable containing the bearer token")
    registry_index.add_argument("--publisher", help="Publisher override")
    registry_index.add_argument("--description", help="Description override")
    registry_index.add_argument("--artifact-url", help="Artifact URL override")
    registry_index.add_argument("--artifact-checksum", help="Artifact SHA-256 override")
    registry_index.add_argument("--artifact-signature-url", help="Detached signature URL override")
    registry_index.add_argument("--publisher-key-fingerprint", help="Publisher GPG key fingerprint override")
    registry_index.add_argument("--dry-run", action="store_true", help="Print the index request without sending it")

    return parser


def _add_common_args(parser: argparse.ArgumentParser, *, source_required: bool = True) -> None:
    if source_required:
        parser.add_argument("source", help="Package source: local directory, ZIP, URL, Maven coordinate, or registry source")
    else:
        parser.add_argument("source", nargs="?", help="Package source: local directory, ZIP, URL, Maven coordinate, or registry source")
    parser.add_argument("--approve", action="store_true", help="Approve policy-gated actions")
    parser.add_argument(
        "--package",
        dest="package",
        help="Package name or application name to select when source is a workspace root",
    )
    parser.add_argument(
        "--registry-url",
        default=None,
        help="Registry base URL for registry: sources, default: DBPM_REGISTRY_URL or https://registry.dbpm.io",
    )


def _add_policy_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--policy",
        choices=("locked", "unlocked"),
        help="Deployment policy for disconnected planning; connected plans read CORE/DEPLOY_LOCKED",
    )


def _add_deploy_environment_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--deploy-environment",
        help="Core DEPLOY_ENVIRONMENT value for bootstrap-core before Core can be read",
    )


def _add_execution_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="Print the plan without executing")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed source resolution and database inspection progress",
    )
    parser.add_argument(
        "--runtime-prefix",
        default=None,
        help="Application-level target prefix for the complete runtime dependency graph",
    )
    _add_database_args(parser)


def _add_dependency_source_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dependency-source",
        action="append",
        default=[],
        help="Package source that may satisfy a manifest dependency",
    )


def _add_database_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--connect",
        default=os.environ.get("DBPM_CONNECT"),
        help=(
            "Raw SQL*Plus/SQLcl connect string; defaults to DBPM_CONNECT, then to a string "
            "composed from DBPM_DB_USER, DBPM_DB_PASSWORD, and DBPM_DB_DSN"
        ),
    )
    parser.add_argument(
        "--connect-name",
        default=os.environ.get("DBPM_CONNECT_NAME"),
        help="SQLcl saved connection name, default: DBPM_CONNECT_NAME",
    )
    parser.add_argument(
        "--runner",
        default=os.environ.get("DBPM_SQL_RUNNER", "sqlplus"),
        help="SQL runner executable, default: DBPM_SQL_RUNNER or sqlplus",
    )


def _build_plan(
    mode: str,
    args: argparse.Namespace,
    *,
    include_installed_state: bool = False,
    show_progress: bool = False,
) -> dict[str, object]:
    _report_plan_progress(show_progress, "Loading root package source...")
    source_arg, workspace, selected_workspace_package = _resolve_workspace_source_arg(args.source, args)
    source = load_package_source(source_arg, registry_url=getattr(args, "registry_url", None))
    explicit_dependency_sources = list(getattr(args, "dependency_source", []))
    workspace_sources = workspace_dependency_sources(
        workspace,
        selected_workspace_package,
        explicit_dependency_sources,
    )
    dependency_source_args = [*workspace_sources, *explicit_dependency_sources]
    dependency_sources = []
    for raw_path in dependency_source_args:
        _report_plan_progress(show_progress, f"Loading dependency source {raw_path}...")
        dependency_sources.append(
            load_package_source(raw_path, registry_url=getattr(args, "registry_url", None))
        )
    _report_plan_progress(show_progress, "Resolving package provenance...")
    provenance = resolve_provenance(source)
    if args.command == "dev" and source.source_type != "directory":
        raise DbpmError("dev reset requires a mutable local directory source")
    if _has_database_access(args) and mode != "bootstrap-core":
        _report_plan_progress(show_progress, "Reading Core deployment policy...")
    environment = _resolve_policy_for_plan(mode, args)
    allow_destructive = bool(getattr(args, "allow_destructive", False))
    confirm_delete_system = getattr(args, "confirm_delete_system", None) == source.manifest.application_name
    installed_state = None
    reverse_dependencies = None
    if (
        include_installed_state
        and source.manifest.has_database_component
        and _should_read_installed_state(mode, source.manifest.is_core)
    ):
        _report_plan_progress(
            show_progress,
            f"Reading installed state for {source.manifest.application_name}...",
        )
        installed_state = _get_installed_state(args, source.manifest.application_name)
        if not source.manifest.is_core:
            _report_plan_progress(
                show_progress,
                f"Reading reverse dependencies for {source.manifest.application_name}...",
            )
            reverse_dependencies = _get_reverse_dependencies(args, source.manifest.application_name)

    graph_reinstall = mode == "reinstall" and getattr(args, "cascade", None) == "graph"
    required_capabilities: list[str] = []
    if graph_reinstall:
        required_capabilities.append("DBPM_ALLOW_GRAPH_RESET")
    if args.command == "dev" and installed_state is None:
        raise DbpmError(
            f"{source.manifest.application_name} is not installed; dev reset requires an installed application"
        )
    if (
        args.command == "dev"
        and installed_state is not None
        and installed_state.get("version") != source.manifest.version
    ):
        raise DbpmError(
            f"dev reset is for same-version replacement; installed "
            f"{installed_state.get('version')}, selected {source.manifest.version}"
        )
    if args.command == "dev":
        required_capabilities.extend((
            "DBPM_ALLOW_MUTABLE_SOURCE",
            "DBPM_ALLOW_SAME_VERSION_REPLACE",
        ))
    if mode == "reinstall" and installed_state is not None and not source.manifest.is_core:
        if source.source_type == "directory":
            required_capabilities.append("DBPM_ALLOW_MUTABLE_SOURCE")
        if installed_state.get("version") == source.manifest.version:
            required_capabilities.append("DBPM_ALLOW_SAME_VERSION_REPLACE")
            if source.source_type != "directory" and _has_database_access(args):
                recorded = get_deployment_provenance(
                    connect=_connect_spec(args), runner=args.runner,
                    application_name=source.manifest.application_name,
                    version=source.manifest.version,
                )
                recorded_checksum = recorded.get("artifact_checksum") if isinstance(recorded, dict) else None
                if (
                    recorded_checksum
                    and source.artifact_checksum
                    and recorded_checksum != source.artifact_checksum
                ):
                    raise DbpmError(
                        f"Immutable artifact identity conflict for {source.manifest.application_name} "
                        f"{source.manifest.version}: recorded checksum {recorded_checksum}, "
                        f"selected checksum {source.artifact_checksum}"
                    )
    if mode == "reinstall" and any(
        item.manifest.runtime is not None for item in [source, *dependency_sources]
    ):
        required_capabilities.append("DBPM_ALLOW_RUNTIME_REPLACE")

    if args.command in {"plan", "install", "lock", "upgrade", "validate", "uninstall", "reinstall", "dev"} and (
        dependency_sources or source.manifest.dependencies
    ):
        installed_states = {source.manifest.application_name: installed_state}
        reverse_dependencies_by_app = {source.manifest.application_name: reverse_dependencies or []}
        if include_installed_state:
            for dependency in source.manifest.dependencies:
                app_name = _application_name(dependency.name)
                _report_plan_progress(show_progress, f"Reading installed state for {app_name}...")
                installed_states[app_name] = _get_installed_state(args, app_name)
                _report_plan_progress(show_progress, f"Reading reverse dependencies for {app_name}...")
                reverse_dependencies_by_app[app_name] = _get_reverse_dependencies(args, app_name)
            for dependency_source in dependency_sources:
                app_name = dependency_source.manifest.application_name
                _report_plan_progress(show_progress, f"Reading installed state for {app_name}...")
                installed_states[app_name] = _get_installed_state(args, app_name)
                _report_plan_progress(show_progress, f"Reading reverse dependencies for {app_name}...")
                reverse_dependencies_by_app[app_name] = _get_reverse_dependencies(args, app_name)
        _report_plan_progress(show_progress, "Resolving dependency graph...")
        plan = create_multi_package_plan(
            mode=mode,
            source=source,
            dependency_sources=dependency_sources,
            environment=environment,
            installed_states=installed_states,
            reverse_dependencies=reverse_dependencies_by_app,
            allow_destructive=allow_destructive,
            approve=args.approve,
            graph_reinstall=graph_reinstall,
            required_capabilities=tuple(dict.fromkeys(required_capabilities)),
        )
        if graph_reinstall:
            graph_apps = set(plan.get("execution_order", []))
            for child in plan.get("packages", []):
                if isinstance(child, dict):
                    child["reverse_dependencies"] = [
                        name for name in child.get("reverse_dependencies", [])
                        if name not in graph_apps
                    ]
        return plan

    if mode == "upgrade" and installed_state is not None:
        installed_version = installed_state.get("version")
        if isinstance(installed_version, str):
            chain = resolve_upgrade_chain(source, source_arg, installed_version)
            if len(chain) > 1:
                return _build_chain_plan(chain, args, installed_version, environment, allow_destructive)

    return create_plan(
        mode=mode,
        source=source,
        provenance=provenance,
        environment=environment,
        installed_state=installed_state,
        reverse_dependencies=reverse_dependencies,
        allow_destructive=allow_destructive,
        confirm_delete_system=confirm_delete_system,
        approve=args.approve,
        required_capabilities=tuple(dict.fromkeys(required_capabilities)),
    )


def _attach_plan_identity(plan: dict[str, object], surface: str) -> None:
    normalized = dict(plan)
    normalized.pop("audit", None)
    normalized.pop("plan_digest", None)
    plan["plan_digest"] = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    plan["audit"] = {"initiating_surface": surface}


def _attach_target_identity(plan: dict[str, object], args: argparse.Namespace) -> None:
    identity = get_target_identity(connect=_connect_spec(args), runner=args.runner)
    policy = plan.get("policy")
    packages = plan.get("packages")
    if not isinstance(policy, dict) and isinstance(packages, list) and packages:
        first = packages[0]
        policy = first.get("policy") if isinstance(first, dict) else None
    context = policy.get("policy_context") if isinstance(policy, dict) else None
    plan["target"] = {
        "service": identity.service_name,
        "schema": identity.schema_name,
        "core_environment": context.get("deploy_environment") if isinstance(context, dict) else None,
    }


def _consumer_first_order(
    applications: list[str], dependencies: list[tuple[str, str]],
) -> list[str]:
    remaining = set(applications)
    edges = {(consumer, dependency) for consumer, dependency in dependencies if consumer in remaining and dependency in remaining}
    ordered: list[str] = []
    while remaining:
        candidates = sorted(
            app for app in remaining
            if not any(dependency == app and consumer in remaining for consumer, dependency in edges)
        )
        if not candidates:
            raise DbpmError("Installed application dependency graph contains a cycle")
        for app in candidates:
            ordered.append(app)
            remaining.remove(app)
    return ordered


def _collect_receipt_state_rules(receipt: dict[str, object]) -> list[dict[str, object]]:
    rules: list[dict[str, object]] = []
    root_package = receipt.get("package")
    if isinstance(root_package, dict):
        root_state = root_package.get("state")
        if isinstance(root_state, list):
            rules.extend(item for item in root_state if isinstance(item, dict))
    packages = receipt.get("packages")
    if isinstance(packages, list):
        for entry in packages:
            if not isinstance(entry, dict):
                continue
            package = entry.get("package")
            if not isinstance(package, dict):
                continue
            state = package.get("state")
            if isinstance(state, list):
                rules.extend(item for item in state if isinstance(item, dict))
    return rules


def _build_environment_reset_plan(args: argparse.Namespace) -> dict[str, object]:
    environment = _resolve_policy_for_plan("uninstall", args)
    environment.require("DBPM_ALLOW_ENVIRONMENT_RESET")
    core = _get_installed_state(args, "CORE")
    if core is None or core.get("deploy_status") != "C":
        status = None if core is None else core.get("deploy_status")
        raise DbpmError(
            f"CORE is not healthy (status {status or 'not installed'}); repair Core before reset-environment"
        )
    applications, dependencies = get_installed_application_graph(
        connect=_connect_spec(args), runner=args.runner
    )
    removable = [app for app in applications if app.upper() != "CORE"]
    removal_order = _consumer_first_order(removable, dependencies)
    identity = get_target_identity(connect=_connect_spec(args), runner=args.runner)
    purge_categories = sorted(set(getattr(args, "purge_var", None) or []))
    forbidden_purge = set(purge_categories) & NEVER_PURGED_STATE_CATEGORIES
    if forbidden_purge:
        raise DbpmError(
            f"--purge-var cannot select {', '.join(sorted(forbidden_purge))}"
        )
    runtime_removals: list[dict[str, object]] = []
    affected_prefixes: list[str] = []
    preserved_state: dict[str, object] = {}
    for raw_prefix in args.runtime_prefix:
        prefix = Path(raw_prefix).expanduser().resolve()
        receipt = load_lifecycle_receipt(runtime_prefix=str(prefix))
        graph = receipt.get("application_runtime")
        package = receipt.get("package")
        app_name = package.get("application_name") if isinstance(package, dict) else None
        if app_name not in removal_order:
            raise DbpmError(f"Runtime prefix {prefix} belongs to {app_name}, which is not in the reset plan")
        state_rules = _collect_receipt_state_rules(receipt)
        classification = classify_preserved_state(prefix, state_rules)
        preserved_state[str(app_name)] = {"prefix": str(prefix), **classification}
        if isinstance(graph, dict):
            graph = dict(graph)
            graph["effects"] = dict(graph.get("effects", {}), operation="uninstall")
            runtime_removals.append({
                "prefix": str(prefix),
                "graph": graph,
                "classification": classification,
            })
            affected_prefixes.append(str(prefix))
    unscoped_applications = sorted(str(name) for name in removal_order if str(name) not in preserved_state)
    plan: dict[str, object] = {
        "schema_version": "dbpm.environment-reset.v0",
        "mode": "uninstall",
        "environment_reset": True,
        "keep": ["CORE"],
        "target": {
            "service": identity.service_name,
            "schema": identity.schema_name,
            "core_environment": environment.deploy_environment,
        },
        "removal_order": removal_order,
        "affected_runtime_prefixes": affected_prefixes,
        "runtime_removals": runtime_removals,
        "preserved_paths": ["etc", "var"],
        "preserved_state": preserved_state,
        "purge_categories": purge_categories,
        "unscoped_applications": unscoped_applications,
        "policy": environment.evaluate(
            "uninstall", dirty=False, allow_destructive=True,
            required_capabilities=("DBPM_ALLOW_ENVIRONMENT_RESET",),
        ),
    }
    _attach_plan_identity(plan, "dev reset-environment")
    return plan


def _removal_order_with_reasons(plan: dict[str, object], removal_order: list[object]) -> list[dict[str, object]]:
    reasons: dict[str, str] = {}
    packages = plan.get("packages")
    if isinstance(packages, list):
        for item in packages:
            if not isinstance(item, dict):
                continue
            item_package = item.get("package")
            app_name = item_package.get("application_name") if isinstance(item_package, dict) else None
            if isinstance(app_name, str):
                reasons[app_name] = str(item.get("installation_reason", "MANUAL"))
    return [
        {"application_name": name, "reason": reasons.get(str(name), "REGISTERED")}
        for name in removal_order
    ]


def _confirm_destructive_plan(plan: dict[str, object], args: argparse.Namespace) -> None:
    target = plan.get("target") if isinstance(plan.get("target"), dict) else {}
    package = plan.get("package") if isinstance(plan.get("package"), dict) else {}
    removal_order = plan.get("removal_order") or [package.get("application_name")]
    summary = {
        "service": target.get("service", "connected database"),
        "schema": target.get("schema", "connected schema"),
        "core_environment": target.get("core_environment"),
        "root_application": package.get("application_name"),
        "removal_order": _removal_order_with_reasons(plan, removal_order),
        "affected_runtime_prefixes": plan.get("affected_runtime_prefixes")
        or ([args.runtime_prefix] if getattr(args, "runtime_prefix", None) else []),
    }
    unscoped_applications = plan.get("unscoped_applications")
    if isinstance(unscoped_applications, list) and unscoped_applications:
        summary["unscoped_applications"] = unscoped_applications
        report_progress(
            "Warning: no --runtime-prefix supplied for "
            f"{', '.join(str(name) for name in unscoped_applications)}; "
            "their etc/var content will not be classified, reported, or "
            "purged by this reset."
        )
    preserved_state = plan.get("preserved_state")
    if isinstance(preserved_state, dict) and preserved_state:
        unclassified_total = sum(
            len(entry.get("unclassified", []))
            for entry in preserved_state.values()
            if isinstance(entry, dict)
        )
        summary["preserved_state"] = {
            "unclassified_path_count": unclassified_total,
            "applications": sorted(preserved_state),
        }
        purge_categories = plan.get("purge_categories")
        if purge_categories:
            summary["purge_categories"] = purge_categories
            if unclassified_total:
                report_progress(
                    f"Warning: {unclassified_total} unclassified var/etc path(s) "
                    "have no manifest-declared category and will be preserved "
                    "untouched regardless of --purge-var."
                )
    report_progress("Destructive operation summary: " + json.dumps(summary, sort_keys=True))
    expected = getattr(args, "confirm", None)
    if plan.get("environment_reset") is True and not expected:
        raise DbpmError(
            "reset-environment requires --confirm matching the target schema or Core environment label"
        )
    if expected:
        allowed = {str(target.get("schema") or ""), str(target.get("core_environment") or "")}
        if expected not in allowed:
            raise DbpmError("--confirm must match the target schema or Core environment label")
    if getattr(args, "yes", False):
        return
    if not sys.stdin.isatty():
        raise DbpmError("Destructive operation requires interactive confirmation or --yes")
    response = input("Proceed with destructive operation? [y/N] ").strip().lower()
    if response not in {"y", "yes"}:
        raise DbpmError("Destructive operation cancelled")


def _enrich_destructive_preview(plan: dict[str, object], args: argparse.Namespace) -> None:
    graph = plan.get("application_runtime")
    if not isinstance(graph, dict):
        return
    runtime_prefix = getattr(args, "runtime_prefix", None)
    if not runtime_prefix:
        raise DbpmError("Application runtime requires --runtime-prefix")
    classifications = validate_application_runtime_collisions(
        graph,
        prefix=Path(runtime_prefix).expanduser().resolve(),
        mode="reinstall",
    )
    plan["runtime_preflight"] = {
        "classifications": list(classifications),
        "preserved_paths": ["etc", "var"],
    }


def _enforce_plan_policies(plan: dict[str, object]) -> None:
    packages = plan.get("packages")
    if isinstance(packages, list):
        for child in packages:
            if isinstance(child, dict):
                _execute_or_explain_policy(child)
        return
    _execute_or_explain_policy(plan)


def _build_installed_uninstall_plan(args: argparse.Namespace) -> dict[str, object]:
    if args.source is None and not args.application:
        raise DbpmError("Source-free uninstall requires --application")
    if args.cascade == "graph":
        raise DbpmError(
            "--cascade graph requires the Core capability DBPM_ALLOW_GRAPH_RESET; "
            "that capability is not available in phase 1"
        )
    installed = load_lifecycle_receipt(runtime_prefix=args.runtime_prefix)
    environment = _resolve_policy_for_plan("uninstall", args)
    package = installed.get("package")
    recorded_app = package.get("application_name") if isinstance(package, dict) else None
    if args.application and str(recorded_app or "").upper() != args.application.upper():
        raise DbpmError(
            f"Installed lifecycle receipt is for {recorded_app}, not {args.application.upper()}"
        )
    plan = installed
    plans = plan.get("packages")
    package_plans = plans if isinstance(plans, list) else [plan]
    selected: list[dict[str, object]] = []
    removal_apps = {str(recorded_app)}
    for item in reversed(package_plans):
        if not isinstance(item, dict):
            continue
        reason = item.get("installation_reason", "MANUAL")
        item_package = item.get("package")
        item_app_name = (
            str(item_package.get("application_name")) if isinstance(item_package, dict) else None
        )
        is_root = item_app_name == str(recorded_app)
        if not is_root:
            if args.cascade != "unused" or reason != "AUTO_DEPENDENCY":
                continue
            external = set(_get_reverse_dependencies(args, item_app_name)).difference(removal_apps)
            if external:
                continue
        lifecycle = item.get("lifecycle")
        uninstall = lifecycle.get("uninstall") if isinstance(lifecycle, dict) else None
        updated = dict(item)
        updated["mode"] = "uninstall"
        updated["policy"] = environment.evaluate(
            "uninstall",
            dirty=False,
            allow_destructive=bool(args.allow_destructive) if is_root else False,
            approve=args.approve,
        )
        updated["pre_actions"] = []
        updated["post_actions"] = []
        updated["execution"] = {
            "script": uninstall.get("path") if isinstance(uninstall, dict) else None,
            "script_ref": uninstall.get("ref") if isinstance(uninstall, dict) else None,
            "arguments": [],
            "stdin": None,
        }
        updated["installed_state"] = _get_installed_state(
            args, str(updated.get("package", {}).get("application_name"))
        )
        updated["reverse_dependencies"] = _get_reverse_dependencies(
            args, str(updated.get("package", {}).get("application_name"))
        )
        selected.append(updated)
        if item_app_name is not None:
            removal_apps.add(item_app_name)
    result = dict(plan)
    result["mode"] = "uninstall"
    result["execution_order"] = [
        item["package"]["application_name"] for item in selected if isinstance(item.get("package"), dict)
    ]
    if isinstance(plans, list):
        result["packages"] = selected
    else:
        result = selected[0]
    full_removal = len(selected) == len(package_plans)
    runtime = result.get("application_runtime")
    if isinstance(runtime, dict):
        if full_removal:
            if not args.runtime_prefix:
                raise DbpmError("Application runtime requires --runtime-prefix")
            runtime["effects"] = dict(runtime.get("effects", {}), operation="uninstall")
        else:
            report_progress(
                "Skipping application runtime teardown: cascade removal does not cover "
                "the full application; runtime state is left in place."
            )
            result["application_runtime"] = None
    return result


def _build_installed_resume_plan(
    args: argparse.Namespace, *, allow_completed: bool = False,
) -> dict[str, object]:
    if args.source is None and not args.application:
        raise DbpmError("Source-free resume requires --application")
    if not args.runtime_prefix:
        raise DbpmError("Runtime recovery requires --runtime-prefix")
    installed = load_lifecycle_receipt(runtime_prefix=args.runtime_prefix)
    package = installed.get("package")
    recorded_app = package.get("application_name") if isinstance(package, dict) else None
    if args.application and str(recorded_app or "").upper() != args.application.upper():
        raise DbpmError(
            f"Installed lifecycle receipt is for {recorded_app}, not {args.application.upper()}"
        )
    operation = get_current_operation(
        connect=_connect_spec(args), runner=args.runner, application_name=str(recorded_app)
    )
    if operation is None:
        raise DbpmError(f"No recoverable composite operation exists for {recorded_app}")
    if operation.state == "VALIDATED" and not allow_completed:
        raise DbpmError(f"Operation {operation.operation_id} is already complete")
    environment = _resolve_policy_for_plan("resume", args)
    result = dict(installed)
    plans = result.get("packages")
    package_plans = plans if isinstance(plans, list) else [result]
    updated_plans: list[dict[str, object]] = []
    for item in package_plans:
        if not isinstance(item, dict):
            continue
        updated = dict(item)
        lifecycle = updated.get("lifecycle")
        app = updated.get("package")
        app_name = app.get("application_name") if isinstance(app, dict) else None
        fresh_state = _get_installed_state(args, str(app_name))
        # A package the composite operation has not reached yet (no APPLICATION
        # row) has nothing to resume; treat it like a fresh install instead of
        # rejecting the whole resume, since get_current_operation() above
        # already confirmed a recoverable operation exists for the plan.
        #
        # For a package that IS already underway, resume the same script the
        # in-flight operation started with (e.g. `upgrade`), not always
        # `install` -- an install script can legitimately refuse to run
        # against a schema its own upgrade already touched.
        resumed_script_key = operation.mode if fresh_state is not None else "install"
        script = (
            lifecycle.get(resumed_script_key, lifecycle.get("install"))
            if isinstance(lifecycle, dict)
            else None
        )
        updated["mode"] = "resume" if fresh_state is not None else "install"
        updated["policy"] = environment.evaluate("resume", dirty=False, approve=args.approve)
        updated["execution"] = {
            "script": script.get("path") if isinstance(script, dict) else None,
            "script_ref": script.get("ref") if isinstance(script, dict) else None,
            "arguments": list(updated.get("execution", {}).get("arguments", [])),
            "stdin": None,
        }
        updated["installed_state"] = fresh_state
        updated["operation_resume"] = True
        updated_plans.append(updated)
    result["mode"] = "resume"
    result["operation"] = {
        "operation_id": operation.operation_id,
        "resume_existing": True,
    }
    if isinstance(plans, list):
        result["packages"] = updated_plans
    else:
        result.update(updated_plans[0])
        result["operation"] = {
            "operation_id": operation.operation_id,
            "resume_existing": True,
        }
    return result


def _resolve_workspace_source_arg(
    raw_source: str | None,
    args: argparse.Namespace,
) -> tuple[str, object | None, object | None]:
    selector = getattr(args, "package", None)
    source_text = raw_source
    if source_text is None and selector:
        source_text = "."
    if source_text is None:
        raise DbpmError("Source is required")

    if _is_remote_or_coordinate_source(source_text):
        return source_text, None, None

    source_path = Path(source_text).expanduser()
    if not source_path.exists():
        return source_text, None, None
    source_path = source_path.resolve()
    if is_workspace_root(source_path):
        workspace = load_workspace(source_path)
        selected = select_workspace_package(workspace, selector)
        return str(selected.path), workspace, selected
    return source_text, None, None


def _is_remote_or_coordinate_source(value: str) -> bool:
    return value.startswith(("registry:", "gh-maven:", "maven:", "http://", "https://"))


def _build_plan_from_lockfile(
    args: argparse.Namespace,
    *,
    include_installed_state: bool = False,
    show_progress: bool = False,
) -> dict[str, object]:
    if args.source is not None or getattr(args, "dependency_source", []):
        raise DbpmError("--lockfile cannot be combined with source or --dependency-source")

    _report_plan_progress(show_progress, "Loading dependency lockfile...")
    lockfile_path = Path(args.lockfile)
    lockfile = load_lockfile(lockfile_path)
    root_entry, dep_entries = lockfile_package_sources_with_checksums(lockfile)

    root_uri, root_checksum, root_alg, root_sig_url, root_publisher_key = root_entry
    _report_plan_progress(show_progress, "Loading locked root package source...")
    root_source = load_package_source(
        root_uri,
        expected_checksum=root_checksum,
        expected_checksum_alg=root_alg,
        expected_signature_url=root_sig_url,
        expected_publisher_key_fingerprint=root_publisher_key,
    )
    dep_sources = []
    for uri, checksum, alg, sig_url, publisher_key in dep_entries:
        _report_plan_progress(show_progress, f"Loading locked dependency source {uri}...")
        dep_sources.append(load_package_source(
            uri,
            expected_checksum=checksum,
            expected_checksum_alg=alg,
            expected_signature_url=sig_url,
            expected_publisher_key_fingerprint=publisher_key,
        ))

    if _has_database_access(args):
        _report_plan_progress(show_progress, "Reading Core deployment policy...")
    environment = _resolve_policy_for_plan("install", args)
    _report_plan_progress(show_progress, "Validating locked dependency graph...")
    resolution_plan = create_multi_package_plan(
        mode="install",
        source=root_source,
        dependency_sources=dep_sources,
        environment=environment,
        installed_states={},
        reverse_dependencies={},
        allow_destructive=False,
        approve=args.approve,
    )
    assert_lockfile_matches_plan(lockfile, resolution_plan)

    installed_states: dict[str, dict[str, str] | None] = {}
    reverse_dependencies_by_app: dict[str, list[str]] = {}

    if include_installed_state:
        for source in [root_source, *dep_sources]:
            app_name = source.manifest.application_name
            if _should_read_installed_state("install", source.manifest.is_core):
                _report_plan_progress(show_progress, f"Reading installed state for {app_name}...")
                installed_states[app_name] = _get_installed_state(args, app_name)
                _report_plan_progress(show_progress, f"Reading reverse dependencies for {app_name}...")
                reverse_dependencies_by_app[app_name] = _get_reverse_dependencies(args, app_name)

    _report_plan_progress(show_progress, "Resolving dependency graph...")
    plan = create_multi_package_plan(
        mode="install",
        source=root_source,
        dependency_sources=dep_sources,
        environment=environment,
        installed_states=installed_states,
        reverse_dependencies=reverse_dependencies_by_app,
        allow_destructive=False,
        approve=args.approve,
    )
    return plan


def _report_plan_progress(enabled: bool, message: str) -> None:
    if enabled:
        report_progress(message)


def _resolve_policy_for_plan(mode: str, args: argparse.Namespace) -> DeploymentPolicy:
    cli_policy = getattr(args, "policy", None)
    deploy_environment = getattr(args, "deploy_environment", None)
    if deploy_environment is not None and mode != "bootstrap-core":
        raise DbpmError("--deploy-environment is only supported for bootstrap-core")
    has_database_access = _has_database_access(args)
    if mode != "bootstrap-core" and has_database_access:
        if cli_policy is not None:
            raise DbpmError(
                "--policy is only supported without database access; "
                "connected plans read CORE/DEPLOY_LOCKED"
            )
        metadata = get_core_deployment_metadata(connect=_connect_spec(args), runner=args.runner)
        return policy_from_core_values(
            deploy_locked=metadata.deploy_locked,
            deploy_environment=metadata.deploy_environment,
            capabilities=metadata.capabilities,
        )
    return resolve_deployment_policy(
        cli_policy,
        source="cli-policy" if cli_policy else "default",
        deploy_environment=deploy_environment,
    )


def _build_chain_plan(
    chain: list,
    args: argparse.Namespace,
    installed_version: str,
    environment: DeploymentPolicy,
    allow_destructive: bool,
) -> dict[str, object]:
    from .provenance import resolve_provenance
    steps = []
    modeled_version = installed_version
    for step_source in chain:
        modeled_state = {"version": modeled_version, "deploy_status": "C"}
        step_plan = create_plan(
            mode="upgrade",
            source=step_source,
            provenance=resolve_provenance(step_source),
            environment=environment,
            installed_state=modeled_state,
            reverse_dependencies=None,
            allow_destructive=allow_destructive,
            approve=args.approve,
        )
        steps.append(step_plan)
        modeled_version = step_source.manifest.version

    target = chain[-1]
    return {
        "schema_version": "dbpm.upgrade-chain.v0",
        "mode": "upgrade",
        "package": {
            "name": target.manifest.name,
            "application_name": target.manifest.application_name,
            "version": target.manifest.version,
        },
        "installed_version": installed_version,
        "steps": steps,
    }


def _execute_or_explain(plan: dict[str, object], args: argparse.Namespace) -> None:
    if plan.get("schema_version") == "dbpm.upgrade-chain.v0":
        _execute_upgrade_chain(plan, args)
        return

    connect = _connect_spec(args) if _plan_needs_database(plan) else None
    runtime_prefix = getattr(args, "runtime_prefix", None)
    packages = plan.get("packages")
    if isinstance(packages, list):
        report_progress(f"Checking database and policy state for {len(packages)} packages...")
        allow_dependent_break = getattr(args, "allow_dependent_break", False)
        for child_plan in packages:
            if not isinstance(child_plan, dict):
                raise DbpmError("Multi-package plan entries must be objects")
            if getattr(args, "verbose", False):
                report_progress(f"Checking {_package_progress_identity(child_plan)}...")
            _execute_or_explain_policy(child_plan)
            _enforce_installed_state(child_plan)
            _enforce_core_minimum_version(child_plan, args)
            _enforce_major_upgrade_dependencies(child_plan, allow_dependent_break)
        if plan.get("mode") in {"bootstrap-core", "install", "upgrade", "reinstall", "resume"}:
            plan = snapshot_plan(plan, runtime_prefix=runtime_prefix)
        plan = _prepare_composite_plan(plan, runtime_prefix=runtime_prefix, connect=connect)
        execute_plan(plan, connect=connect, runner=args.runner, runtime_prefix=runtime_prefix)
        if (
            plan.get("mode") in {"bootstrap-core", "install", "upgrade", "reinstall"}
            and not isinstance(plan.get("operation"), dict)
        ):
            write_lifecycle_receipt(plan, runtime_prefix=runtime_prefix)
        return

    if getattr(args, "verbose", False):
        report_progress(f"Checking {_package_progress_identity(plan)}...")
    else:
        report_progress("Checking database and policy state...")
    _execute_or_explain_policy(plan)
    _enforce_installed_state(plan)
    _enforce_core_minimum_version(plan, args)
    _enforce_major_upgrade_dependencies(plan, getattr(args, "allow_dependent_break", False))
    if plan.get("mode") in {"bootstrap-core", "install", "upgrade", "reinstall", "resume"}:
        plan = snapshot_plan(plan, runtime_prefix=runtime_prefix)
    plan = _prepare_composite_plan(plan, runtime_prefix=runtime_prefix, connect=connect)
    execute_plan(plan, connect=connect, runner=args.runner, runtime_prefix=runtime_prefix)
    if (
        plan.get("mode") in {"bootstrap-core", "install", "upgrade", "reinstall"}
        and not isinstance(plan.get("operation"), dict)
    ):
        write_lifecycle_receipt(plan, runtime_prefix=runtime_prefix)


def _prepare_composite_plan(
    plan: dict[str, object], *, runtime_prefix: str | None, connect: ConnectSpec | None,
) -> dict[str, object]:
    if connect is None or not isinstance(plan.get("application_runtime"), dict):
        return plan
    mode = str(plan.get("mode") or "")
    if mode not in {"install", "upgrade", "reinstall", "resume"}:
        return plan
    operation = plan.get("operation")
    if not isinstance(operation, dict):
        operation = {"operation_id": str(uuid.uuid4()), "resume_existing": False}
        plan["operation"] = operation
    operation["runtime_prefix"] = str(Path(runtime_prefix).expanduser().resolve()) if runtime_prefix else None
    digest_input = dict(plan)
    digest_input.pop("operation", None)
    operation["plan_digest"] = str(plan.get("plan_digest") or hashlib.sha256(
        json.dumps(digest_input, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest())
    audit = plan.get("audit")
    if isinstance(audit, dict):
        operation["initiating_surface"] = audit.get("initiating_surface")
    if mode != "resume":
        receipt_path = write_lifecycle_receipt(plan, runtime_prefix=runtime_prefix)
        operation["receipt_checksum"] = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    return plan


def _package_progress_identity(plan: dict[str, object]) -> str:
    package = plan.get("package")
    if not isinstance(package, dict):
        return "package"
    name = package.get("name") or package.get("application_name") or "package"
    version = package.get("version")
    return f"{name} {version}" if isinstance(version, str) and version else str(name)


def _report_execution_success(command: str, plan: dict[str, object]) -> None:
    action = command.replace("-", " ").capitalize()
    report_progress(
        f"{action} completed successfully: {_package_progress_identity(plan)}"
    )


def _plan_needs_database(plan: dict[str, object]) -> bool:
    packages = plan.get("packages")
    if isinstance(packages, list):
        return any(
            _plan_needs_database(child_plan)
            for child_plan in packages
            if isinstance(child_plan, dict)
        )
    execution = plan.get("execution")
    if isinstance(execution, dict) and execution.get("script"):
        return True
    if plan.get("pre_actions") or plan.get("post_actions"):
        return True
    core = plan.get("core")
    if isinstance(core, dict) and core.get("minimum_version"):
        return True
    return False


def _execute_upgrade_chain(plan: dict[str, object], args: argparse.Namespace) -> None:
    steps = plan.get("steps", [])
    if not isinstance(steps, list):
        raise DbpmError("Upgrade chain plan steps must be a list")
    allow_dependent_break = getattr(args, "allow_dependent_break", False)
    for i, step_plan in enumerate(steps):
        if not isinstance(step_plan, dict):
            raise DbpmError("Upgrade chain step must be an object")
        if i > 0:
            package = step_plan.get("package")
            app_name = package.get("application_name") if isinstance(package, dict) else None
            if isinstance(app_name, str):
                fresh_state = _get_installed_state(args, app_name)
                step_plan = dict(step_plan)
                step_plan["installed_state"] = fresh_state
        _execute_or_explain_policy(step_plan)
        _enforce_installed_state(step_plan)
        _enforce_core_minimum_version(step_plan, args)
        _enforce_major_upgrade_dependencies(step_plan, allow_dependent_break)
        execute_plan(
            step_plan,
            connect=_connect_spec(args),
            runner=args.runner,
            runtime_prefix=getattr(args, "runtime_prefix", None),
        )


def _enforce_major_upgrade_dependencies(
    plan: dict[str, object],
    allow_dependent_break: bool,
) -> None:
    if allow_dependent_break or plan.get("mode") != "upgrade":
        return
    package = plan.get("package")
    state = plan.get("installed_state")
    if not isinstance(package, dict) or not isinstance(state, dict):
        return
    installed_version = state.get("version")
    target_version = package.get("version")
    if not isinstance(installed_version, str) or not isinstance(target_version, str):
        return
    if _major(target_version) <= _major(installed_version):
        return
    reverse_deps = plan.get("reverse_dependencies", [])
    if not reverse_deps:
        return
    app_name = package.get("application_name")
    names = ", ".join(str(n) for n in reverse_deps)
    raise DbpmError(
        f"Cannot upgrade {app_name} from {installed_version} to {target_version}; "
        f"installed dependents may have incompatible constraints: {names}. "
        f"Provide updated dependent versions with --dependency-source, "
        f"or use --allow-dependent-break to override."
    )


def _enforce_core_minimum_version(plan: dict[str, object], args: argparse.Namespace) -> None:
    if plan.get("mode") == "bootstrap-core":
        return
    core = plan.get("core")
    if not isinstance(core, dict):
        return
    required = core.get("minimum_version")
    if not isinstance(required, str):
        return
    installed_state = _get_installed_state(args, "CORE")
    if installed_state is None:
        raise DbpmError(
            f"This package requires Core {required} or newer, but Core is not installed. "
            f"Install Core first with: dbpm bootstrap-core"
        )
    status = installed_state.get("deploy_status")
    if status != "C":
        raise DbpmError(
            f"This package requires Core {required} or newer, but Core deployment "
            f"status is {status}; resume or reinstall Core first."
        )
    installed = installed_state.get("version")
    if not isinstance(installed, str):
        return
    try:
        if parse_version(installed) < parse_version(required):
            raise DbpmError(
                f"This package requires Core {required} or newer; "
                f"Core {installed} is installed. "
                f"Upgrade Core first with: dbpm upgrade <core-source> --connect ..."
            )
    except ValueError:
        pass


def _major(version: str) -> int:
    try:
        return int(version.split(".")[0])
    except (ValueError, IndexError):
        return 0


def _execute_or_explain_policy(plan: dict[str, object]) -> None:
    policy = plan.get("policy")
    if isinstance(policy, dict) and policy.get("result") != "allowed":
        blocked = policy.get("blocked", [])
        approvals = policy.get("required_approvals", [])
        reasons = [*blocked, *approvals] if isinstance(blocked, list) and isinstance(approvals, list) else []
        raise DbpmError("; ".join(str(reason) for reason in reasons) or "Policy blocks execution")


def _get_installed_state(args: argparse.Namespace, application_name: str) -> dict[str, str] | None:
    state = get_application_state(
        connect=_connect_spec(args),
        runner=args.runner,
        application_name=application_name,
    )
    return None if state is None else state.as_dict()


def _should_read_installed_state(mode: str, is_core: bool) -> bool:
    if not is_core:
        return True
    return mode in {"bootstrap-core", "upgrade", "resume", "validate"}


def _get_reverse_dependencies(args: argparse.Namespace, application_name: str) -> list[str]:
    return get_reverse_dependencies(
        connect=_connect_spec(args),
        runner=args.runner,
        application_name=application_name,
    )


def _enforce_installed_state(plan: dict[str, object]) -> None:
    execution = plan.get("execution")
    if isinstance(execution, dict) and not execution.get("script"):
        # Runtime-only package plans have no Core registration; the runtime
        # receipt enforces installed state at execution time instead.
        return
    mode = plan.get("mode")
    state = plan.get("installed_state")
    package = plan.get("package")
    app_name = None
    if isinstance(package, dict):
        app_name = package.get("application_name")

    status = state.get("deploy_status") if isinstance(state, dict) else None

    if mode == "bootstrap-core":
        if state is None:
            return
        raise DbpmError(
            f"{app_name} is already installed with status {status}; "
            f"use upgrade, resume, or reinstall instead of bootstrap-core"
        )

    if mode == "install":
        if state is None:
            return
        if status == "C":
            raise DbpmError(
                f"{app_name} is already installed; use reinstall or upgrade"
                f"{_suggest_commands(plan, ('upgrade', []), ('reinstall', ['--allow-destructive']))}"
            )
        raise DbpmError(
            f"{app_name} deployment status is {status}; use resume or reinstall"
            f"{_suggest_commands(plan, ('resume', []), ('reinstall', ['--allow-destructive']))}"
        )

    if mode == "resume":
        if state is None:
            raise DbpmError(
                f"{app_name} is not installed; use install"
                f"{_suggest_commands(plan, ('install', []))}"
            )
        if status == "C" and plan.get("operation_resume") is True:
            return
        if status not in {"R", "F"}:
            raise DbpmError(f"{app_name} deployment status is {status}; resume requires R or F")
        return

    if mode == "validate":
        if state is None:
            raise DbpmError(
                f"{app_name} is not installed; use install"
                f"{_suggest_commands(plan, ('install', []))}"
            )
        if status != "C":
            raise DbpmError(f"{app_name} deployment status is {status}; validate requires C")
        return

    if mode == "upgrade":
        if state is None:
            raise DbpmError(
                f"{app_name} is not installed; use install"
                f"{_suggest_commands(plan, ('install', []))}"
            )
        if status != "C":
            raise DbpmError(
                f"{app_name} deployment status is {status}; upgrade requires C"
                f"{_suggest_commands(plan, ('resume', []), ('reinstall', ['--allow-destructive']))}"
            )
        installed_version = state.get("version") if isinstance(state, dict) else None
        target_version = package.get("version") if isinstance(package, dict) else None
        if installed_version and target_version:
            cmp = _compare_versions(installed_version, target_version)
            if cmp == 0:
                raise DbpmError(
                    f"{app_name} version {target_version} is already installed; no upgrade needed"
                )
            if cmp > 0:
                raise DbpmError(
                    f"Cannot downgrade {app_name} from {installed_version} to {target_version}"
                )
        return

    if mode == "reinstall":
        reverse_dependencies = plan.get("reverse_dependencies", [])
        if reverse_dependencies:
            names = ", ".join(str(name) for name in reverse_dependencies)
            raise DbpmError(f"Cannot reinstall {app_name}; installed applications depend on it: {names}")
        return

    if isinstance(state, dict) and status != "C":
        raise DbpmError(f"{app_name} deployment status is {status}; expected C")


def _suggest_commands(plan: dict[str, object], *commands: tuple[str, list[str]]) -> str:
    source = _suggestion_source_arg(plan)
    if source is None:
        return ""
    lines = [
        "Try one of:",
        *(_format_suggested_command(command, source, extra_args) for command, extra_args in commands),
    ]
    return "\n" + "\n".join(lines)


def _suggestion_source_arg(plan: dict[str, object]) -> str | None:
    source = plan.get("source")
    if not isinstance(source, dict):
        return None
    path = source.get("path")
    if not isinstance(path, str) or not path:
        return None
    return path


def _format_suggested_command(command: str, source: str, extra_args: list[str]) -> str:
    parts = ["dbpm", command, source, *extra_args]
    return "  " + " ".join(shlex.quote(part) for part in parts)


def _has_database_access(args: argparse.Namespace) -> bool:
    return bool(
        getattr(args, "connect", None)
        or getattr(args, "connect_name", None)
        or any(os.environ.get(name) for name in DB_CONNECT_ENV_NAMES)
    )


def _connect_spec(args: argparse.Namespace) -> str | ConnectSpec:
    connect = getattr(args, "connect", None)
    connect_name = getattr(args, "connect_name", None)
    database_values = {name: os.environ.get(name) for name in DB_CONNECT_ENV_NAMES}
    supplied_database_values = {name: value for name, value in database_values.items() if value}
    if supplied_database_values and len(supplied_database_values) != len(DB_CONNECT_ENV_NAMES):
        missing = [name for name in DB_CONNECT_ENV_NAMES if not database_values[name]]
        raise DbpmError(
            "Structured database credentials are incomplete; set " + ", ".join(missing)
        )
    has_structured_connect = len(supplied_database_values) == len(DB_CONNECT_ENV_NAMES)
    if sum(bool(value) for value in (connect, connect_name, has_structured_connect)) > 1:
        raise DbpmError(CONNECT_OPTIONS_CONFLICT_MESSAGE)
    if connect_name:
        spec = sqlcl_name(connect_name)
    elif connect:
        spec = connect_string(connect)
    elif has_structured_connect:
        spec = connect_string(
            f"{database_values['DBPM_DB_USER']}/{database_values['DBPM_DB_PASSWORD']}"
            f"@{database_values['DBPM_DB_DSN']}"
        )
    else:
        raise DbpmError(
            "Database access requires --connect/DBPM_CONNECT, "
            "DBPM_DB_USER/DBPM_DB_PASSWORD/DBPM_DB_DSN, or "
            "--connect-name/DBPM_CONNECT_NAME"
        )
    validate_connect_spec(connect=spec, runner=args.runner)
    return spec if spec.kind == "sqlcl-name" else spec.value


def _application_name(name: str) -> str:
    return name.replace("-", "_").upper()


def _compare_versions(a: str, b: str) -> int:
    """Return negative if a < b, 0 if equal, positive if a > b."""
    def _parts(v: str) -> tuple[int, ...]:
        try:
            return tuple(int(x) for x in v.split("."))
        except ValueError:
            return (0,)

    pa, pb = _parts(a), _parts(b)
    length = max(len(pa), len(pb))
    pa = pa + (0,) * (length - len(pa))
    pb = pb + (0,) * (length - len(pb))
    for x, y in zip(pa, pb):
        if x != y:
            return x - y
    return 0


def _print_json(value: dict[str, object]) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
