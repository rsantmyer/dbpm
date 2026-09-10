from __future__ import annotations

from dataclasses import asdict
import re
from .environment import DeploymentPolicy
from .errors import ManifestError
from .manifest import PackageManifest
from .provenance import Provenance
from .source import PackageSource


CORE_UNINSTALL_SCRIPT = "Deployment_Manifests/uninstall.core.sql"
RUNTIME_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


def create_plan(
    *,
    mode: str,
    source: PackageSource,
    provenance: Provenance,
    environment: DeploymentPolicy,
    installed_state: dict[str, str] | None = None,
    reverse_dependencies: list[str] | None = None,
    allow_destructive: bool = False,
    confirm_delete_system: bool = False,
    approve: bool = False,
    required_capabilities: tuple[str, ...] = (),
    resume_as: str | None = None,
) -> dict[str, object]:
    manifest = source.manifest
    policy = environment.evaluate(
        mode,
        dirty=provenance.dirty,
        allow_destructive=allow_destructive,
        approve=approve,
        required_capabilities=required_capabilities,
    )
    policy = _apply_core_reinstall_policy(
        policy,
        mode=mode,
        manifest=manifest,
        confirm_delete_system=confirm_delete_system,
    )
    script = _script_for_mode(mode, manifest, resume_as=resume_as)
    runtime_package = _application_runtime_package(manifest, source, provenance)
    if (
        mode in {"bootstrap-core", "install", "reinstall", "resume", "upgrade", "validate"}
        and not script
        and runtime_package is None
    ):
        raise ManifestError(f"No script is declared for deployment mode `{mode}`")

    plan: dict[str, object] = {
        "schema_version": "dbpm.plan.v0",
        "mode": mode,
        "package": _package_dict(manifest),
        "source": {
            "type": source.source_type,
            "path": source.display_path,
            "root": source.root,
            "manifest": source.manifest_name,
            "registry_url": source.registry_url,
            "registry_package": source.registry_package,
            "registry_constraint": source.registry_constraint,
            "checksum": source.artifact_checksum,
            "checksum_alg": source.artifact_checksum_alg,
            "work_path": str(source.work_path) if source.work_path else None,
        },
        "core": {
            "required": not manifest.is_core,
            "minimum_version": manifest.core_minimum_version,
            "bootstrap": mode == "bootstrap-core",
        },
        "dependencies": [asdict(dependency) for dependency in manifest.dependencies],
        "reverse_dependencies": reverse_dependencies or [],
        "installed_state": installed_state,
        "warnings": source.warnings or [],
        "provenance": provenance.as_dict(),
        "policy": policy,
        "pre_actions": _pre_actions_for_mode(mode, manifest, source, provenance, installed_state),
        "post_actions": _post_actions_for_mode(mode, manifest, source, provenance, installed_state),
        "execution": {
            "script": script,
            "script_ref": str(source.resolve_script_path(script)) if script else None,
            "arguments": _script_arguments_for_mode(mode, provenance) if script else [],
            "stdin": _script_stdin_for_mode(mode, manifest, environment) if script else None,
        },
        "lifecycle": {
            name: {
                "path": path,
                "ref": str(source.resolve_script_path(path)) if path else None,
            }
            for name, path in {
                "install": manifest.scripts.install,
                "upgrade": manifest.scripts.upgrade,
                "validate": manifest.scripts.validate,
                "uninstall": manifest.scripts.uninstall,
            }.items()
        },
        "runtime_package": runtime_package,
    }
    if runtime_package is not None and not manifest.dependencies:
        plan["application_runtime"] = create_application_runtime_graph_plan(
            [plan],
            root_package_name=manifest.name,
            root_package_version=manifest.version,
            mode=mode,
        )
    return plan


def _package_dict(manifest: PackageManifest) -> dict[str, object]:
    return {
        "name": manifest.name,
        "application_name": manifest.application_name,
        "version": manifest.version,
        "description": manifest.description,
        "vendor": manifest.vendor,
        "license": manifest.license,
        "database": {
            "platform": manifest.database_platform,
            "minimum_version": manifest.database_minimum_version,
        },
        "dbpm": {"minimum_version": manifest.dbpm_minimum_version},
        "state": [
            {"path": entry.path, "category": entry.category} for entry in manifest.state
        ],
    }


def _apply_core_reinstall_policy(
    policy: dict[str, object],
    *,
    mode: str,
    manifest: PackageManifest,
    confirm_delete_system: bool,
) -> dict[str, object]:
    if mode != "reinstall" or not manifest.is_core or confirm_delete_system:
        return policy

    updated = dict(policy)
    approvals = list(updated.get("required_approvals", []))
    approvals.append("Core reinstall requires --confirm-delete-system CORE")
    updated["required_approvals"] = approvals
    updated["result"] = "blocked" if updated.get("blocked") else "requires-approval"
    return updated


def _script_for_mode(
    mode: str, manifest: PackageManifest, *, resume_as: str | None = None
) -> str | None:
    if mode == "resume" and resume_as == "upgrade":
        return manifest.scripts.upgrade
    if mode in {"install", "reinstall", "resume", "bootstrap-core"}:
        return manifest.scripts.install
    if mode == "upgrade":
        return manifest.scripts.upgrade
    if mode == "validate":
        return manifest.scripts.validate
    if mode == "uninstall":
        return manifest.scripts.uninstall
    return None


def _application_runtime_package(
    manifest: PackageManifest,
    source: PackageSource,
    provenance: Provenance,
) -> dict[str, object] | None:
    runtime = manifest.runtime
    if runtime is None:
        return None
    scripts = {
        "install": runtime.install,
        "upgrade": runtime.upgrade,
        "validate": runtime.validate,
        "uninstall": runtime.uninstall,
    }
    return {
        "package": manifest.name,
        "version": manifest.version,
        "payload_path": _runtime_payload_path(manifest),
        "package_root": str(source.work_path or source.path),
        "artifact": {
            "uri": source.display_path,
            "checksum": source.artifact_checksum,
            "checksum_alg": source.artifact_checksum_alg,
            "commit": provenance.commit,
        },
        "scripts": {
            name: {
                "path": path,
                "ref": str(source.resolve_script_path(path)) if path else None,
            }
            for name, path in scripts.items()
        },
        "exports": {
            "commands": [
                {
                    "name": item.name,
                    "target": item.target,
                    "canonical": f"{manifest.name}.{item.name}",
                }
                for item in runtime.command_exports
            ]
        },
        "activation": {
            "commands": {
                "aliases": {
                    item.export: item.name for item in runtime.command_aliases
                },
                "disabled": list(runtime.disabled_commands),
            }
        },
    }


def create_application_runtime_graph_plan(
    package_plans: list[dict[str, object]],
    *,
    root_package_name: str,
    root_package_version: str,
    mode: str = "install",
) -> dict[str, object]:
    runtime_packages: list[dict[str, object]] = []
    root_runtime: dict[str, object] | None = None
    for package_plan in package_plans:
        package = package_plan.get("package")
        package_name = package.get("name") if isinstance(package, dict) else None
        runtime_package = package_plan.get("runtime_package")
        if isinstance(runtime_package, dict):
            runtime_packages.append(runtime_package)
            if package_name == root_package_name:
                root_runtime = runtime_package

    if not runtime_packages:
        raise ManifestError("Application runtime graph has no application-v1 packages")
    aliases: dict[str, str] = {}
    disabled: set[str] = set()
    if root_runtime is not None:
        activation = root_runtime.get("activation")
        commands = activation.get("commands") if isinstance(activation, dict) else None
        if isinstance(commands, dict):
            raw_aliases = commands.get("aliases")
            raw_disabled = commands.get("disabled")
            if isinstance(raw_aliases, dict):
                aliases = {
                    str(canonical): str(name)
                    for canonical, name in raw_aliases.items()
                }
            if isinstance(raw_disabled, list):
                disabled = {str(item) for item in raw_disabled}

    available: dict[str, tuple[dict[str, object], dict[str, object]]] = {}
    for runtime_package in runtime_packages:
        exports = runtime_package.get("exports")
        commands = exports.get("commands") if isinstance(exports, dict) else None
        if not isinstance(commands, list):
            continue
        for command in commands:
            if not isinstance(command, dict):
                continue
            canonical = command.get("canonical")
            if isinstance(canonical, str):
                available[canonical] = (runtime_package, command)

    unknown = (set(aliases) | disabled).difference(available)
    if unknown:
        raise ManifestError(
            "Root runtime activation references unknown command exports: "
            + ", ".join(sorted(unknown))
        )

    activated: list[dict[str, object]] = []
    names: dict[str, str] = {}
    for canonical, (runtime_package, command) in available.items():
        if canonical in disabled:
            continue
        activated_name = aliases.get(canonical, str(command.get("name") or ""))
        previous = names.get(activated_name)
        if previous is not None:
            raise ManifestError(
                f"Runtime command name collision for `{activated_name}` between "
                f"`{previous}` and `{canonical}`; alias or disable one export "
                "in the root application"
            )
        names[activated_name] = canonical
        payload_path = str(runtime_package.get("payload_path") or "")
        target = str(command.get("target") or "")
        activated.append(
            {
                "name": activated_name,
                "canonical": canonical,
                "package": runtime_package.get("package"),
                "export": command.get("name"),
                "target": f"{payload_path}/{target}",
                "link": f"bin/{activated_name}",
            }
        )

    return {
        "schema_version": "dbpm.application-runtime-plan.v1",
        "root_package": root_package_name,
        "root_version": root_package_version,
        "payloads": runtime_packages,
        "commands": activated,
        "effects": {
            "operation": mode,
            "payloads": [
                payload.get("payload_path") for payload in runtime_packages
            ],
            "commands": [command.get("link") for command in activated],
        },
    }


def _runtime_payload_path(manifest: PackageManifest) -> str:
    if not RUNTIME_PATH_SEGMENT_RE.fullmatch(manifest.version):
        raise ManifestError(
            f"`package.version` cannot be used as a runtime path segment: "
            f"{manifest.version!r}"
        )
    return f"packages/{manifest.name}/{manifest.version}"


def _script_arguments_for_mode(mode: str, provenance: Provenance) -> list[str]:
    if mode == "validate":
        return []
    return [provenance.commit]


def _script_stdin_for_mode(mode: str, manifest: PackageManifest, environment: DeploymentPolicy) -> str | None:
    if mode not in {"bootstrap-core", "install", "reinstall"} or not manifest.is_core:
        return None
    deploy_locked = "Y" if environment.deployment_locked else "N"
    deploy_environment = environment.deploy_environment or ""
    return f"{deploy_locked}\n{deploy_environment}\n"


def _pre_actions_for_mode(
    mode: str,
    manifest: PackageManifest,
    source: PackageSource,
    provenance: Provenance,
    installed_state: dict[str, str] | None,
) -> list[dict[str, object]]:
    actions: list[dict[str, object]] = []
    if not manifest.has_database_component:
        return actions
    if mode == "reinstall":
        if manifest.is_core:
            actions.extend(
                [
                    {
                        "type": "delete_system",
                    },
                    {
                        "type": "execute_script",
                        "script": CORE_UNINSTALL_SCRIPT,
                        "script_ref": str(source.resolve_script_path(CORE_UNINSTALL_SCRIPT)),
                        "arguments": [],
                        "stdin": "YES\n",
                    },
                ]
            )
        else:
            actions.append(
                {
                    "type": "delete_application",
                    "application_name": manifest.application_name,
                    "fail_on_not_found": "N",
                }
            )
    if mode in {"install", "reinstall", "resume", "upgrade"} and _can_stage_provenance(
        mode,
        manifest,
        installed_state,
    ):
        actions.append(
            {
                "type": "stage_deployment_provenance",
                "payload": _deployment_provenance_payload(
                    mode=mode,
                    manifest=manifest,
                    source=source,
                    provenance=provenance,
                    installed_state=installed_state,
                ),
            }
        )
    return actions


def _post_actions_for_mode(
    mode: str,
    manifest: PackageManifest,
    source: PackageSource,
    provenance: Provenance,
    installed_state: dict[str, str] | None,
) -> list[dict[str, object]]:
    actions: list[dict[str, object]] = []
    if not manifest.has_database_component:
        return actions
    if mode in {"bootstrap-core", "reinstall"} and _can_record_core_post_deploy_provenance(manifest):
        actions.append(
            {
                "type": "record_deployment_provenance",
                "payload": _deployment_provenance_payload(
                    mode=mode,
                    manifest=manifest,
                    source=source,
                    provenance=provenance,
                    installed_state=installed_state,
                ),
            }
        )
    return actions


def _can_stage_provenance(
    mode: str,
    manifest: PackageManifest,
    installed_state: dict[str, str] | None,
) -> bool:
    if not manifest.is_core:
        return True
    if mode != "upgrade" or installed_state is None:
        return False
    installed_version = installed_state.get("version")
    if installed_version is None:
        return False
    return _parse_semver(installed_version) >= (3, 2, 0)


def _can_record_core_post_deploy_provenance(manifest: PackageManifest) -> bool:
    return manifest.is_core and _parse_semver(manifest.version) >= (3, 4, 0)


def _deployment_provenance_payload(
    *,
    mode: str,
    manifest: PackageManifest,
    source: PackageSource,
    provenance: Provenance,
    installed_state: dict[str, str] | None,
) -> dict[str, object]:
    artifact = provenance.artifact
    coordinate = _package_coordinate(artifact)
    payload: dict[str, object] = {
        "application_name": manifest.application_name,
        "version": manifest.version,
        "deployment_type": _deployment_type_for_mode(mode, manifest, installed_state),
        "deploy_commit_hash": provenance.commit,
        "artifact_uri": source.display_path,
        "artifact_checksum": source.artifact_checksum,
        "artifact_checksum_alg": source.artifact_checksum_alg,
        "artifact_signature_url": source.artifact_signature_url,
        "publisher_key_fingerprint": source.publisher_key_fingerprint,
        "artifact_file_name": source.path.name if source.is_zip else None,
        "artifact_repository_type": "file" if source.is_zip else "local",
        "artifact_group_id": artifact.get("artifact.groupId"),
        "artifact_id": artifact.get("artifact.artifactId"),
        "artifact_version": artifact.get("artifact.version"),
        "artifact_classifier": artifact.get("artifact.classifier"),
        "artifact_extension": artifact.get("artifact.extension") or ("zip" if source.is_zip else None),
        "package_coordinate": coordinate,
        "source_repository_url": artifact.get("git.remote.origin.url"),
        "source_commit_hash": provenance.commit,
        "source_path": source.display_path,
        "build_id": artifact.get("build.id"),
        "build_url": artifact.get("build.url"),
        "build_time": artifact.get("build.time"),
        "build_metadata_json": {
            "source": provenance.source,
            "dirty": provenance.dirty,
            "artifact": artifact,
        },
    }
    return payload


def _deployment_type_for_mode(
    mode: str,
    manifest: PackageManifest,
    installed_state: dict[str, str] | None,
) -> str:
    if mode in {"install", "reinstall", "resume"}:
        return "I"
    if mode == "bootstrap-core":
        return "I"
    if mode == "upgrade" and installed_state:
        installed_version = installed_state.get("version")
        if installed_version:
            installed_major, installed_minor, _ = _parse_semver(installed_version)
            target_major, target_minor, _ = _parse_semver(manifest.version)
            if target_major > installed_major:
                return "V"
            if target_minor > installed_minor:
                return "M"
    return "P"


def _parse_semver(value: str) -> tuple[int, int, int]:
    parts = value.split(".")
    if len(parts) != 3:
        raise ManifestError(f"Version must be major.minor.patch: {value}")
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError as exc:
        raise ManifestError(f"Version must be numeric: {value}") from exc


def _package_coordinate(artifact: dict[str, str]) -> str | None:
    group_id = artifact.get("artifact.groupId")
    artifact_id = artifact.get("artifact.artifactId")
    version = artifact.get("artifact.version")
    if group_id and artifact_id and version:
        return f"{group_id}:{artifact_id}:{version}"
    return None
