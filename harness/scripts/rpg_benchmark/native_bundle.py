"""Bind the delivered RPG hosts to their source/build/public-contract provenance."""
from . import lifecycle
from .runtime import local_path, read_json


def admit(base, binding, identity, renderer, helper):
    if (set(binding) != {"contract", "build_manifest"}
            or set(identity) != {"contract_sha256", "build_manifest_sha256"}
            or renderer is None or helper is None):
        raise ValueError("native RPG bundle requires explicit matching renderer/helper/provenance")
    paths = {key: local_path(base, value) for key, value in binding.items()}
    values = {key: read_json(path, identity[key + "_sha256"]) for key, path in paths.items()}
    build, contract = values["build_manifest"], values["contract"]
    if (contract.get("schema") != "rpg-native-interactions" or contract.get("version") != "1.0"
            or contract.get("native_protocol_version") != "0.2"
            or build.get("source_manifest_sha256") != renderer["source_manifest_sha256"]
            or build.get("public_schema_sha256") != identity["contract_sha256"]):
        raise ValueError("native RPG source/build/public contract mismatch")
    for name, expected in (
            ("debug/veloren-voxygen", renderer["executable_sha256"]),
            ("debug/veloren-server-cli", renderer["server_sha256"]),
            ("debug/examples/agent_bridge", lifecycle.sha(local_path(base, helper["executable"])))):
        if build["binaries"][name]["sha256"] != expected:
            raise ValueError("native RPG host mismatch: " + name)
    return {**identity, "schema": "rpg-native-interactions/1.0",
            "source_tree_sha256": renderer["source_tree_sha256"],
            "private_oracle": "server-owner-file-only"}, list(paths.values())
