#!/usr/bin/env python3
"""
sros2_policy.py - Validation and verification utility for SROS2 security policies.

Enforces:
1. Deny-by-default access control (<default>DENY</default>) across all enclaves.
2. Disallow unauthenticated participants (allow_unauthenticated_participants=FALSE).
3. Enforced encryption on metadata and payloads (ENCRYPT).
4. Strict topic ownership and least-privilege segregation:
   - /ubuntu_tank/controller: may only publish to /ubuntu_tank_safety/motor_input, /odom_raw
   - /ubuntu_tank/guard: sole publisher of /ros_robot_controller/set_motor_guarded, sole provider of /ubuntu_tank_safety/set_arm
   - /ubuntu_tank/bridge: subscriber of /ros_robot_controller/set_motor_guarded; cannot publish motion or arm
   - /ubuntu_tank/operator: publisher of /controller/cmd_vel; caller of /ubuntu_tank_safety/set_arm; no direct motor access
   - /ubuntu_tank/status: read-only status and telemetry subscriber; cannot publish motion or call set_arm
"""

import ctypes
import ctypes.util
import fnmatch
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from typing import Dict, List, Set, Tuple

CONFIG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "sros2"
)
SCHEMAS_DIR = os.path.join(CONFIG_DIR, "schemas")
GOVERNANCE_PATH = os.path.join(CONFIG_DIR, "governance.xml")
POLICIES_PATH = os.path.join(CONFIG_DIR, "policies.xml")
PERMISSIONS_DIR = os.path.join(CONFIG_DIR, "permissions")

GOV_SCHEMA_PATH = os.path.join(SCHEMAS_DIR, "omg_shared_ca_governance.xsd")
PERM_SCHEMA_PATH = os.path.join(SCHEMAS_DIR, "omg_shared_ca_permissions.xsd")

REQUIRED_ENCLAVES = {
    "/ubuntu_tank/controller",
    "/ubuntu_tank/guard",
    "/ubuntu_tank/bridge",
    "/ubuntu_tank/operator",
    "/ubuntu_tank/status",
}

DISALLOWED_BROAD_PATTERNS = {
    "*",
    "*/*",
    "rt/*",
    "rt/**",
    "rq/*",
    "rq/**",
    "rr/*",
    "rr/**",
    "**",
}


def matches_any_dds_pattern(resource: str, patterns: Set[str]) -> bool:
    """Check if resource matches any DDS pattern (exact string or glob pattern)."""
    return any(fnmatch.fnmatchcase(resource, pat) for pat in patterns)


class PolicyValidationError(Exception):
    """Raised when an SROS2 policy fails security invariants."""

    pass


def _load_libxml2():
    """Attempt to resolve and load the libxml2 dynamic library across supported platforms."""
    candidates = []
    found = ctypes.util.find_library("xml2")
    if found:
        candidates.append(found)

    # Official Ubuntu 26.04 ABI (libxml2 >= 2.14, SONAME libxml2.so.16)
    candidates.append("libxml2.so.16")
    # Ubuntu 24.04 / 22.04 / Debian ABI (libxml2 < 2.14, SONAME libxml2.so.2)
    candidates.append("libxml2.so.2")
    # Unversioned library symlink
    candidates.append("libxml2.so")

    seen = set()
    unique_candidates = []
    for cand in candidates:
        if cand and cand not in seen:
            seen.add(cand)
            unique_candidates.append(cand)

    load_errors = []
    for cand in unique_candidates:
        try:
            lib = ctypes.cdll.LoadLibrary(cand)
            if lib:
                return lib, []
        except Exception as exc:
            load_errors.append(f"{cand}: {exc}")

    return None, load_errors


def validate_xml_against_xsd(xml_path: str, xsd_path: str) -> None:
    """Validate an XML file against an official XSD schema using libxml2 or xmllint (fails closed if unavailable)."""
    if not os.path.isfile(xml_path):
        raise PolicyValidationError(f"Missing XML file: {xml_path}")
    if not os.path.isfile(xsd_path):
        raise PolicyValidationError(f"Missing XSD schema file: {xsd_path}")

    libxml2, load_errors = _load_libxml2()
    if libxml2 is not None:
        libxml2.xmlSchemaNewParserCtxt.argtypes = [ctypes.c_char_p]
        libxml2.xmlSchemaNewParserCtxt.restype = ctypes.c_void_p
        libxml2.xmlSchemaParse.argtypes = [ctypes.c_void_p]
        libxml2.xmlSchemaParse.restype = ctypes.c_void_p
        libxml2.xmlSchemaFreeParserCtxt.argtypes = [ctypes.c_void_p]
        libxml2.xmlSchemaNewValidCtxt.argtypes = [ctypes.c_void_p]
        libxml2.xmlSchemaNewValidCtxt.restype = ctypes.c_void_p
        libxml2.xmlSchemaValidateFile.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        libxml2.xmlSchemaValidateFile.restype = ctypes.c_int
        libxml2.xmlSchemaFreeValidCtxt.argtypes = [ctypes.c_void_p]
        libxml2.xmlSchemaFree.argtypes = [ctypes.c_void_p]

        parser_ctxt = libxml2.xmlSchemaNewParserCtxt(xsd_path.encode("utf-8"))
        if not parser_ctxt:
            raise PolicyValidationError(
                f"Failed to create schema parser context for {xsd_path}"
            )
        schema = libxml2.xmlSchemaParse(parser_ctxt)
        libxml2.xmlSchemaFreeParserCtxt(parser_ctxt)
        if not schema:
            raise PolicyValidationError(f"Failed to parse XSD schema {xsd_path}")

        valid_ctxt = libxml2.xmlSchemaNewValidCtxt(schema)
        if not valid_ctxt:
            libxml2.xmlSchemaFree(schema)
            raise PolicyValidationError(
                f"Failed to create validation context for {xsd_path}"
            )

        res = libxml2.xmlSchemaValidateFile(valid_ctxt, xml_path.encode("utf-8"), 0)
        libxml2.xmlSchemaFreeValidCtxt(valid_ctxt)
        libxml2.xmlSchemaFree(schema)

        if res != 0:
            raise PolicyValidationError(
                f"XML file {xml_path} failed official XSD schema validation against {xsd_path} (code: {res})"
            )
        return

    # Fallback: check for locked xmllint CLI executable (from locked libxml2-utils)
    xmllint = shutil.which("xmllint")
    if xmllint:
        res = subprocess.run(
            [xmllint, "--noout", "--schema", xsd_path, xml_path],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            detail = res.stderr.strip() or res.stdout.strip()
            raise PolicyValidationError(
                f"XML file {xml_path} failed official XSD schema validation via xmllint against {xsd_path}: {detail}"
            )
        return

    err_detail = "; ".join(load_errors) if load_errors else "no candidate library found"
    raise PolicyValidationError(
        f"Official XSD schema validator unavailable: failed to load libxml2 ({err_detail}) and xmllint not found in PATH. "
        "libxml2 or xmllint is required for strict OMG DDS security schema validation."
    )


def verify_official_schemas():
    """Validate all governance and permissions files against official OMG XSD schemas."""
    validate_xml_against_xsd(GOVERNANCE_PATH, GOV_SCHEMA_PATH)
    for enc in ["controller", "guard", "bridge", "operator", "status"]:
        perm_path = os.path.join(PERMISSIONS_DIR, f"{enc}_permissions.xml")
        validate_xml_against_xsd(perm_path, PERM_SCHEMA_PATH)


def verify_governance(governance_file: str = GOVERNANCE_PATH) -> bool:
    """Validate DDS Security governance policy rules."""
    if not os.path.isfile(governance_file):
        raise PolicyValidationError(f"Missing governance file: {governance_file}")

    tree = ET.parse(governance_file)
    root = tree.getroot()

    # Check unauthenticated participants rule (must be false)
    allow_unauth = root.findall(".//allow_unauthenticated_participants")
    if not allow_unauth or any(
        elem.text.strip().lower() != "false" for elem in allow_unauth
    ):
        raise PolicyValidationError(
            "Governance must reject unauthenticated participants (false)"
        )

    # Check join access control (must be true)
    join_ctrl = root.findall(".//enable_join_access_control")
    if not join_ctrl or any(elem.text.strip().lower() != "true" for elem in join_ctrl):
        raise PolicyValidationError("Governance must enable join access control (true)")

    # Check domain protection levels
    for tag in [
        "discovery_protection_kind",
        "liveliness_protection_kind",
        "rtps_protection_kind",
    ]:
        elems = root.findall(f".//{tag}")
        if not elems or any(elem.text.strip().upper() != "ENCRYPT" for elem in elems):
            raise PolicyValidationError(f"Governance must enforce ENCRYPT for {tag}")

    # Check topic rules
    topic_rules = root.findall(".//topic_rule")
    if not topic_rules:
        raise PolicyValidationError("Governance must define at least one topic_rule")
    for tr in topic_rules:
        for tag in [
            "enable_discovery_protection",
            "enable_liveliness_protection",
            "enable_read_access_control",
            "enable_write_access_control",
        ]:
            elem = tr.find(f"./{tag}")
            if elem is None or elem.text.strip().lower() != "true":
                raise PolicyValidationError(f"Topic rule must enable {tag} (true)")
        for tag in ["metadata_protection_kind", "data_protection_kind"]:
            elem = tr.find(f"./{tag}")
            if elem is None or elem.text.strip().upper() != "ENCRYPT":
                raise PolicyValidationError(
                    f"Topic rule must enforce ENCRYPT for {tag}"
                )

    topic_exprs = {
        tr.find("./topic_expression").text.strip()
        for tr in topic_rules
        if tr.find("./topic_expression") is not None
        and tr.find("./topic_expression").text
    }
    if "ros_discovery_info" not in topic_exprs:
        raise PolicyValidationError(
            "Governance must define topic_rule for ros_discovery_info"
        )

    return True


def parse_permissions_xml(file_path: str) -> Dict[str, any]:
    """Parse DDS permissions XML to extract grant rules."""
    tree = ET.parse(file_path)
    root = tree.getroot()

    grant_elem = root.find(".//grant")
    if grant_elem is None:
        raise PolicyValidationError(f"No <grant> element in {file_path}")

    grant_name = grant_elem.get("name")
    default_rule = grant_elem.find("./default")
    default_action = default_rule.text.strip() if default_rule is not None else None

    pub_topics = set()
    for topic_elem in root.findall(".//publish/topics/topic"):
        pub_topics.add(topic_elem.text.strip())

    sub_topics = set()
    for topic_elem in root.findall(".//subscribe/topics/topic"):
        sub_topics.add(topic_elem.text.strip())

    return {
        "name": grant_name,
        "default": default_action,
        "publish_topics": pub_topics,
        "subscribe_topics": sub_topics,
    }


def parse_policies_xml(
    file_path: str = POLICIES_PATH,
) -> Dict[str, Dict[str, Set[str]]]:
    """Parse ROS 2 SROS2 policies.xml to extract per-enclave permissions."""
    if not os.path.isfile(file_path):
        raise PolicyValidationError(f"Missing policies file: {file_path}")

    tree = ET.parse(file_path)
    root = tree.getroot()

    enclaves = {}
    for enclave_elem in root.findall(".//enclave"):
        path = enclave_elem.get("path")
        pub_topics = set()
        sub_topics = set()
        service_replies = set()
        service_requests = set()

        for profile in enclave_elem.findall(".//profile"):
            for top in profile.findall('.//topics[@publish="ALLOW"]/topic'):
                pub_topics.add(top.text.strip())
            for top in profile.findall('.//topics[@subscribe="ALLOW"]/topic'):
                sub_topics.add(top.text.strip())
            for srv in profile.findall('.//services[@reply="ALLOW"]/service'):
                service_replies.add(srv.text.strip())
            for srv in profile.findall('.//services[@request="ALLOW"]/service'):
                service_requests.add(srv.text.strip())

        enclaves[path] = {
            "publish_topics": pub_topics,
            "subscribe_topics": sub_topics,
            "service_replies": service_replies,
            "service_requests": service_requests,
        }

    return enclaves


def verify_security_invariants(
    policies_path: str = POLICIES_PATH, permissions_dir: str = PERMISSIONS_DIR
) -> bool:
    """Verify SROS2 least privilege and safety isolation invariants."""
    enclaves = parse_policies_xml(policies_path)

    # 1. Verify all required enclaves are defined
    missing = REQUIRED_ENCLAVES - set(enclaves.keys())
    if missing:
        raise PolicyValidationError(f"Missing required enclaves: {missing}")

    # 2. Guard topic invariant: Only /ubuntu_tank/guard can publish to /ros_robot_controller/set_motor_guarded
    guarded_topic = "/ros_robot_controller/set_motor_guarded"
    for enc, perms in enclaves.items():
        if enc == "/ubuntu_tank/guard":
            if guarded_topic not in perms["publish_topics"]:
                raise PolicyValidationError(
                    "Guard must be allowed to publish to guarded motor topic"
                )
        else:
            if guarded_topic in perms["publish_topics"]:
                raise PolicyValidationError(
                    f"Security violation: Non-guard enclave {enc} may publish to guarded topic"
                )

    # 3. Guard input invariant: Only /ubuntu_tank/controller can publish to /ubuntu_tank_safety/motor_input
    motor_input_topic = "/ubuntu_tank_safety/motor_input"
    for enc, perms in enclaves.items():
        if enc == "/ubuntu_tank/controller":
            if motor_input_topic not in perms["publish_topics"]:
                raise PolicyValidationError(
                    "Controller must be allowed to publish to /ubuntu_tank_safety/motor_input"
                )
        else:
            if motor_input_topic in perms["publish_topics"]:
                raise PolicyValidationError(
                    f"Security violation: Non-controller enclave {enc} may publish to guard input"
                )

    # 4. Arming service invariant: Only /ubuntu_tank/guard offers /ubuntu_tank_safety/set_arm
    arm_service = "/ubuntu_tank_safety/set_arm"
    for enc, perms in enclaves.items():
        if enc == "/ubuntu_tank/guard":
            if arm_service not in perms["service_replies"]:
                raise PolicyValidationError(
                    "Guard must offer /ubuntu_tank_safety/set_arm service"
                )
        else:
            if arm_service in perms["service_replies"]:
                raise PolicyValidationError(
                    f"Security violation: Enclave {enc} offers /ubuntu_tank_safety/set_arm"
                )

    # 5. Arming caller invariant: Only operator can call /ubuntu_tank_safety/set_arm
    for enc, perms in enclaves.items():
        if enc == "/ubuntu_tank/operator":
            if arm_service not in perms["service_requests"]:
                raise PolicyValidationError(
                    "Operator must be allowed to call /ubuntu_tank_safety/set_arm"
                )
        else:
            if arm_service in perms["service_requests"]:
                raise PolicyValidationError(
                    f"Security violation: Enclave {enc} allowed to call /ubuntu_tank_safety/set_arm"
                )

    # 6. Status read-only invariant: status enclave must NOT have motion publications or arm requests
    status_perms = enclaves["/ubuntu_tank/status"]
    forbidden_status_topics = {
        "/controller/cmd_vel",
        "/ubuntu_tank_safety/motor_input",
        "/ros_robot_controller/set_motor_guarded",
    }
    if status_perms["publish_topics"].intersection(forbidden_status_topics):
        raise PolicyValidationError(
            "Status enclave must not publish to any motion command topic"
        )
    if status_perms["service_requests"]:
        raise PolicyValidationError("Status enclave must not call any services")

    # 7. Verify DDS permissions files enforce DENY-by-default and topic isolation
    dds_perms = {}
    for enc_name in ["controller", "guard", "bridge", "operator", "status"]:
        perm_file = os.path.join(permissions_dir, f"{enc_name}_permissions.xml")
        if not os.path.isfile(perm_file):
            raise PolicyValidationError(f"Missing permissions file: {perm_file}")
        perm_data = parse_permissions_xml(perm_file)
        if perm_data["default"] != "DENY":
            raise PolicyValidationError(
                f"Enclave {enc_name} does not enforce DENY-by-default (found: {perm_data['default']})"
            )
        # Forbid overly broad wildcard patterns
        all_patterns = perm_data["publish_topics"] | perm_data["subscribe_topics"]
        broad_matches = all_patterns & DISALLOWED_BROAD_PATTERNS
        if broad_matches:
            raise PolicyValidationError(
                f"Security violation: Enclave {enc_name} contains disallowed broad wildcard pattern(s): {broad_matches}"
            )
        # Check that middleware discovery topic is granted in both directions
        if not matches_any_dds_pattern(
            "ros_discovery_info", perm_data["publish_topics"]
        ):
            raise PolicyValidationError(
                f"Enclave {enc_name} missing publish permission for ros_discovery_info"
            )
        if not matches_any_dds_pattern(
            "ros_discovery_info", perm_data["subscribe_topics"]
        ):
            raise PolicyValidationError(
                f"Enclave {enc_name} missing subscribe permission for ros_discovery_info"
            )
        dds_perms[enc_name] = perm_data

    # 8. Verify DDS permissions topic-level isolation
    guarded_dds = "rt/ros_robot_controller/set_motor_guarded"
    input_dds = "rt/ubuntu_tank_safety/motor_input"
    arm_req_dds = "rq/ubuntu_tank_safety/set_armRequest"

    if not matches_any_dds_pattern(guarded_dds, dds_perms["guard"]["publish_topics"]):
        raise PolicyValidationError(
            f"Guard permissions must allow publishing {guarded_dds}"
        )
    for name, pdata in dds_perms.items():
        if name != "guard" and matches_any_dds_pattern(
            guarded_dds, pdata["publish_topics"]
        ):
            raise PolicyValidationError(
                f"DDS permissions violation: {name} allowed to publish {guarded_dds}"
            )

    if not matches_any_dds_pattern(
        input_dds, dds_perms["controller"]["publish_topics"]
    ):
        raise PolicyValidationError(
            f"Controller permissions must allow publishing {input_dds}"
        )
    for name, pdata in dds_perms.items():
        if name != "controller" and matches_any_dds_pattern(
            input_dds, pdata["publish_topics"]
        ):
            raise PolicyValidationError(
                f"DDS permissions violation: {name} allowed to publish {input_dds}"
            )

    if not matches_any_dds_pattern(
        arm_req_dds, dds_perms["operator"]["publish_topics"]
    ):
        raise PolicyValidationError(
            f"Operator permissions must allow publishing {arm_req_dds}"
        )
    for name, pdata in dds_perms.items():
        if name != "operator" and matches_any_dds_pattern(
            arm_req_dds, pdata["publish_topics"]
        ):
            raise PolicyValidationError(
                f"DDS permissions violation: {name} allowed to publish {arm_req_dds}"
            )

    status_forbidden_dds = ["rt/controller/cmd_vel", input_dds, guarded_dds]
    for f_topic in status_forbidden_dds:
        if matches_any_dds_pattern(f_topic, dds_perms["status"]["publish_topics"]):
            raise PolicyValidationError(
                f"Status DDS permissions must not publish to motion command topic: {f_topic}"
            )
    for pat in dds_perms["status"]["publish_topics"]:
        if pat.startswith("rq/") or fnmatch.fnmatchcase("rq/any", pat):
            raise PolicyValidationError(
                "Status DDS permissions must not publish service requests"
            )

    obs_dds = "rt/ubuntu_tank/delivery_observation"
    for pub_node in ["controller", "guard", "bridge"]:
        if not matches_any_dds_pattern(obs_dds, dds_perms[pub_node]["publish_topics"]):
            raise PolicyValidationError(
                f"Enclave {pub_node} must be allowed to publish {obs_dds}"
            )
    for sub_node in ["operator", "status"]:
        if not matches_any_dds_pattern(
            obs_dds, dds_perms[sub_node]["subscribe_topics"]
        ):
            raise PolicyValidationError(
                f"Enclave {sub_node} must be allowed to subscribe to {obs_dds}"
            )
        if matches_any_dds_pattern(obs_dds, dds_perms[sub_node]["publish_topics"]):
            raise PolicyValidationError(
                f"Enclave {sub_node} must not be allowed to publish {obs_dds}"
            )

    batt_dds = "rt/ros_robot_controller/battery"
    for sub_node in ["operator", "status"]:
        if not matches_any_dds_pattern(
            batt_dds, dds_perms[sub_node]["subscribe_topics"]
        ):
            raise PolicyValidationError(
                f"Enclave {sub_node} must be allowed to subscribe to {batt_dds}"
            )
        if matches_any_dds_pattern(batt_dds, dds_perms[sub_node]["publish_topics"]):
            raise PolicyValidationError(
                f"Enclave {sub_node} must not be allowed to publish {batt_dds}"
            )

    # 9. Verify actuator topic access is strictly denied to operator and status
    for restricted_node in ["operator", "status"]:
        for act_topic in [input_dds, guarded_dds]:
            if matches_any_dds_pattern(
                act_topic, dds_perms[restricted_node]["publish_topics"]
            ):
                raise PolicyValidationError(
                    f"Security violation: Enclave {restricted_node} may publish actuator topic {act_topic}"
                )
            if matches_any_dds_pattern(
                act_topic, dds_perms[restricted_node]["subscribe_topics"]
            ):
                raise PolicyValidationError(
                    f"Security violation: Enclave {restricted_node} may subscribe to actuator topic {act_topic}"
                )

    return True


def simulate_participant_access(
    enclave: str, action: str, resource: str, permissions_dir: str = PERMISSIONS_DIR
) -> bool:
    """
    Simulate SROS2 / DDS security access decision for an enclave participant.
    Evaluates both high-level ROS policy and actual low-level DDS permissions.
    action: 'publish_topic', 'subscribe_topic', 'request_service', 'reply_service'
    """
    enclaves = parse_policies_xml()
    if enclave not in enclaves:
        return False  # Uncredentialed or unknown enclave rejected by default

    enc_short = enclave.rstrip("/").split("/")[-1]
    perm_file = os.path.join(permissions_dir, f"{enc_short}_permissions.xml")
    if not os.path.isfile(perm_file):
        return False
    dds_perm = parse_permissions_xml(perm_file)
    if dds_perm["default"] != "DENY":
        return False

    perms = enclaves[enclave]

    # Map ROS resource to DDS topic name
    if resource.startswith("/"):
        if action in ("publish_topic", "subscribe_topic"):
            dds_resource = f"rt{resource}"
        elif action == "request_service":
            dds_resource = f"rq{resource}Request"
        elif action == "reply_service":
            dds_resource = f"rr{resource}Reply"
        else:
            dds_resource = resource
    else:
        dds_resource = resource

    if action == "publish_topic":
        ros_ok = resource in perms["publish_topics"]
        dds_ok = matches_any_dds_pattern(dds_resource, dds_perm["publish_topics"])
        return ros_ok and dds_ok
    elif action == "subscribe_topic":
        ros_ok = resource in perms["subscribe_topics"]
        dds_ok = matches_any_dds_pattern(dds_resource, dds_perm["subscribe_topics"])
        return ros_ok and dds_ok
    elif action == "request_service":
        ros_ok = resource in perms["service_requests"]
        dds_ok = matches_any_dds_pattern(dds_resource, dds_perm["publish_topics"])
        return ros_ok and dds_ok
    elif action == "reply_service":
        ros_ok = resource in perms["service_replies"]
        dds_ok = matches_any_dds_pattern(dds_resource, dds_perm["publish_topics"])
        return ros_ok and dds_ok
    return False


def verify_traffic_simulation():
    """Verify that uncredentialed and non-owner participants cannot bypass access controls."""
    # 1. Uncredentialed / unknown participant must be rejected on all operations
    unauth = "/unauthenticated_attacker"
    if simulate_participant_access(
        unauth, "publish_topic", "/ros_robot_controller/set_motor_guarded"
    ):
        raise PolicyValidationError(
            "Unauthenticated participant allowed to publish to guarded motor topic"
        )
    if simulate_participant_access(
        unauth, "request_service", "/ubuntu_tank_safety/set_arm"
    ):
        raise PolicyValidationError(
            "Unauthenticated participant allowed to request arming"
        )
    if simulate_participant_access(unauth, "publish_topic", "/controller/cmd_vel"):
        raise PolicyValidationError(
            "Unauthenticated participant allowed to publish to cmd_vel"
        )

    # 2. Non-guard enclaves cannot publish to guarded motor topic
    for enc in [
        "/ubuntu_tank/controller",
        "/ubuntu_tank/bridge",
        "/ubuntu_tank/operator",
        "/ubuntu_tank/status",
    ]:
        if simulate_participant_access(
            enc, "publish_topic", "/ros_robot_controller/set_motor_guarded"
        ):
            raise PolicyValidationError(
                f"Enclave {enc} allowed to publish directly to guarded motor topic"
            )

    # 3. Non-controller enclaves cannot publish to guard motor input
    for enc in [
        "/ubuntu_tank/guard",
        "/ubuntu_tank/bridge",
        "/ubuntu_tank/operator",
        "/ubuntu_tank/status",
    ]:
        if simulate_participant_access(
            enc, "publish_topic", "/ubuntu_tank_safety/motor_input"
        ):
            raise PolicyValidationError(
                f"Enclave {enc} allowed to publish to guard motor input"
            )

    # 4. Non-operator enclaves cannot call arming service
    for enc in [
        "/ubuntu_tank/controller",
        "/ubuntu_tank/guard",
        "/ubuntu_tank/bridge",
        "/ubuntu_tank/status",
    ]:
        if simulate_participant_access(
            enc, "request_service", "/ubuntu_tank_safety/set_arm"
        ):
            raise PolicyValidationError(f"Enclave {enc} allowed to call arming service")

    # 5. Legitimate flows must succeed
    if not simulate_participant_access(
        "/ubuntu_tank/guard", "publish_topic", "/ros_robot_controller/set_motor_guarded"
    ):
        raise PolicyValidationError(
            "Guard denied permission to publish guarded motor commands"
        )
    if not simulate_participant_access(
        "/ubuntu_tank/controller", "publish_topic", "/ubuntu_tank_safety/motor_input"
    ):
        raise PolicyValidationError(
            "Controller denied permission to publish motor input"
        )
    if not simulate_participant_access(
        "/ubuntu_tank/operator", "request_service", "/ubuntu_tank_safety/set_arm"
    ):
        raise PolicyValidationError("Operator denied permission to call arming service")
    if not simulate_participant_access(
        "/ubuntu_tank/operator", "publish_topic", "/controller/cmd_vel"
    ):
        raise PolicyValidationError("Operator denied permission to publish cmd_vel")
    if not simulate_participant_access(
        "/ubuntu_tank/status", "subscribe_topic", "/ubuntu_tank_safety/state"
    ):
        raise PolicyValidationError(
            "Status denied permission to subscribe to guard state"
        )

    # 6. Verify CLI and dedicated client infrastructure endpoints
    op_perm = parse_permissions_xml(
        os.path.join(PERMISSIONS_DIR, "operator_permissions.xml")
    )
    for topic in [
        "rq/ubuntu_tank_safety/set_armRequest",
        "rr/operator_client/describe_parametersReply",
        "rr/_ros2cli_requester_std_srvs_SetBool/describe_parametersReply",
        "rr/_ros2cli_requester_std_srvs_SetBool/get_type_descriptionReply",
    ]:
        if not matches_any_dds_pattern(topic, op_perm["publish_topics"]):
            raise PolicyValidationError(
                f"Operator permissions missing required publication topic: {topic}"
            )

    for topic in [
        "rr/ubuntu_tank_safety/set_armReply",
        "rq/operator_client/describe_parametersRequest",
        "rq/_ros2cli_requester_std_srvs_SetBool/describe_parametersRequest",
        "rq/_ros2cli_requester_std_srvs_SetBool/get_type_descriptionRequest",
    ]:
        if not matches_any_dds_pattern(topic, op_perm["subscribe_topics"]):
            raise PolicyValidationError(
                f"Operator permissions missing required subscription topic: {topic}"
            )

    st_perm = parse_permissions_xml(
        os.path.join(PERMISSIONS_DIR, "status_permissions.xml")
    )
    for topic in [
        "rr/status_client/describe_parametersReply",
        "rr/_ros2cli_direct_node/get_type_descriptionReply",
    ]:
        if not matches_any_dds_pattern(topic, st_perm["publish_topics"]):
            raise PolicyValidationError(
                f"Status permissions missing required publication topic: {topic}"
            )

    for topic in [
        "rq/status_client/describe_parametersRequest",
        "rq/_ros2cli_direct_node/get_type_descriptionRequest",
        "rt/ubuntu_tank_safety/state",
        "rt/ubuntu_tank_safety/armed",
        "rt/ros_robot_controller/battery",
    ]:
        if not matches_any_dds_pattern(topic, st_perm["subscribe_topics"]):
            raise PolicyValidationError(
                f"Status permissions missing required subscription topic: {topic}"
            )

    return True


def main():
    try:
        verify_official_schemas()
        verify_governance()
        verify_security_invariants()
        verify_traffic_simulation()
        print(
            "SROS2 Security Policy Verification: PASSED (all schemas, governance, enclave invariants, and traffic simulations hold)."
        )
        return 0
    except PolicyValidationError as e:
        sys.stderr.write(f"SROS2 Security Policy Verification FAILED: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
