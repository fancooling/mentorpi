#!/usr/bin/env python3
"""
fastdds_setup.py - Shared Fast DDS loopback discovery contract helper.

Implements the contract defined in Section 6.4.1 of MENTORPI_FRESH_CONTROLLER_DESIGN.md:
- Packages and resolves the root-owned, read-only loopback.xml profile.
- Configures SYSTEM_DEFAULT discovery range, RMW rmw_fastrtps_cpp, domain 0, localhost-only.
- Validates XML structure, transports, and locators.
- Enforces failure closed on missing, unreadable, or invalid profiles.
"""

import os
import sys
from typing import Dict, List, Optional, Tuple
import xml.etree.ElementTree as ET


def resolve_loopback_profile(
    opt_dir: str = "/opt/ubuntu_tank",
    base_dir: Optional[str] = None,
    release_id: Optional[str] = None,
    allow_repo_fallback: bool = True,
) -> str:
    """
    Resolve the absolute path to loopback.xml.

    Candidate search precedence:
    1. /opt/ubuntu_tank/releases/<release_id>/config/fastdds/loopback.xml (if release_id given)
    2. Real path of /opt/ubuntu_tank/current/config/fastdds/loopback.xml
    3. base_dir/config/fastdds/loopback.xml (workspace or staging root)
    4. Relative to this script: ../config/fastdds/loopback.xml (only if base_dir is None and allow_repo_fallback)
    5. Existing FASTDDS_DEFAULT_PROFILES_FILE in os.environ (if file exists)

    Fails closed (raises FileNotFoundError) if no valid profile exists.
    """
    candidates = []

    if release_id:
        candidates.append(
            os.path.join(
                opt_dir, "releases", release_id, "config", "fastdds", "loopback.xml"
            )
        )

    current_link = os.path.join(opt_dir, "current")
    if os.path.islink(current_link):
        try:
            real_target = os.path.realpath(current_link)
            candidates.append(
                os.path.join(real_target, "config", "fastdds", "loopback.xml")
            )
        except Exception:
            pass
    candidates.append(
        os.path.join(opt_dir, "current", "config", "fastdds", "loopback.xml")
    )

    if base_dir:
        candidates.append(os.path.join(base_dir, "config", "fastdds", "loopback.xml"))
    elif allow_repo_fallback:
        # Relative to this script
        this_dir = os.path.abspath(os.path.dirname(__file__))
        candidates.append(
            os.path.abspath(
                os.path.join(this_dir, "..", "config", "fastdds", "loopback.xml")
            )
        )

    env_prof = os.environ.get("FASTDDS_DEFAULT_PROFILES_FILE")
    if env_prof and os.path.isfile(env_prof):
        candidates.append(os.path.abspath(env_prof))

    for cand in candidates:
        if os.path.isfile(cand) and os.access(cand, os.R_OK):
            return os.path.realpath(cand)

    raise FileNotFoundError(
        f"Fast DDS loopback profile loopback.xml could not be resolved from candidates:\n"
        + "\n".join(f"  - {c}" for c in candidates)
    )


def validate_loopback_profile(profile_path: str) -> Tuple[bool, List[str]]:
    """
    Validate the XML schema and contents of a loopback.xml profile.

    Ensures:
    - Well-formed XML with <dds> and <profiles>
    - transport_descriptor type UDPv4 with interfaceWhiteList 127.0.0.1
    - participant rtps useBuiltinTransports=false
    - defaultUnicastLocatorList contains 127.0.0.1
    - metatrafficUnicastLocatorList contains 127.0.0.1
    - initialPeersList contains 127.0.0.1
    """
    errors: List[str] = []
    if not os.path.isfile(profile_path):
        return False, [f"Profile file not found at '{profile_path}'"]

    try:
        tree = ET.parse(profile_path)
        root = tree.getroot()
    except Exception as e:
        return False, [f"XML syntax error in '{profile_path}': {e}"]

    # Strip namespace if present for tag matching
    def strip_ns(tag: str) -> str:
        return tag.split("}")[-1] if "}" in tag else tag

    if strip_ns(root.tag) != "dds":
        errors.append(f"Root element must be <dds>, found <{root.tag}>")

    profiles = None
    for child in root:
        if strip_ns(child.tag) == "profiles":
            profiles = child
            break

    if profiles is None:
        errors.append("Missing <profiles> element in <dds>")
        return False, errors

    # Check transport_descriptors
    found_loopback_transport = False
    for td_container in profiles:
        if strip_ns(td_container.tag) == "transport_descriptors":
            for td in td_container:
                if strip_ns(td.tag) == "transport_descriptor":
                    t_type = None
                    whitelist_addrs = []
                    for prop in td:
                        tag_name = strip_ns(prop.tag)
                        if tag_name == "type":
                            t_type = (prop.text or "").strip()
                        elif tag_name == "interfaceWhiteList":
                            for addr in prop:
                                if strip_ns(addr.tag) == "address":
                                    whitelist_addrs.append((addr.text or "").strip())
                    if t_type == "UDPv4" and "127.0.0.1" in whitelist_addrs:
                        found_loopback_transport = True

    if not found_loopback_transport:
        errors.append(
            "Missing UDPv4 <transport_descriptor> with interfaceWhiteList '127.0.0.1'"
        )

    # Check participant configuration
    found_valid_participant = False
    for part in profiles:
        if strip_ns(part.tag) == "participant":
            rtps = None
            for child in part:
                if strip_ns(child.tag) == "rtps":
                    rtps = child
                    break
            if rtps is not None:
                has_no_builtin = False
                has_unicast_loc = False
                has_meta_loc = False
                has_initial_peers = False

                for elem in rtps:
                    etag = strip_ns(elem.tag)
                    if (
                        etag == "useBuiltinTransports"
                        and (elem.text or "").strip().lower() == "false"
                    ):
                        has_no_builtin = True
                    elif etag == "defaultUnicastLocatorList":
                        text_xml = ET.tostring(elem, encoding="unicode")
                        if "127.0.0.1" in text_xml:
                            has_unicast_loc = True
                    elif etag == "builtin":
                        for bchild in elem:
                            btag = strip_ns(bchild.tag)
                            btext = ET.tostring(bchild, encoding="unicode")
                            if (
                                btag == "metatrafficUnicastLocatorList"
                                and "127.0.0.1" in btext
                            ):
                                has_meta_loc = True
                            elif btag == "initialPeersList" and "127.0.0.1" in btext:
                                has_initial_peers = True

                if (
                    has_no_builtin
                    and has_unicast_loc
                    and has_meta_loc
                    and has_initial_peers
                ):
                    found_valid_participant = True

    if not found_valid_participant:
        errors.append(
            "Missing or incomplete <participant> configuration: requires "
            "useBuiltinTransports=false, loopback defaultUnicastLocatorList, "
            "metatrafficUnicastLocatorList, and initialPeersList containing 127.0.0.1"
        )

    return len(errors) == 0, errors


def get_loopback_env(profile_path: str) -> Dict[str, str]:
    """Return the dictionary of environment variables defined by the discovery contract."""
    return {
        "RMW_IMPLEMENTATION": "rmw_fastrtps_cpp",
        "ROS_DOMAIN_ID": "0",
        "ROS_LOCALHOST_ONLY": "1",
        "ROS_AUTOMATIC_DISCOVERY_RANGE": "SYSTEM_DEFAULT",
        "FASTDDS_DEFAULT_PROFILES_FILE": profile_path,
        "ROS_SECURITY_ENABLE": "true",
        "ROS_SECURITY_STRATEGY": "Enforce",
    }


def apply_loopback_env(
    opt_dir: str = "/opt/ubuntu_tank",
    base_dir: Optional[str] = None,
    release_id: Optional[str] = None,
    profile_path: Optional[str] = None,
) -> Dict[str, str]:
    """
    Resolve, validate, and apply the Fast DDS loopback discovery contract to os.environ.

    Fails closed if the profile cannot be found or is malformed.
    Overrides conflicting inherited discovery variables.
    """
    if not profile_path:
        profile_path = resolve_loopback_profile(
            opt_dir=opt_dir, base_dir=base_dir, release_id=release_id
        )

    valid, errs = validate_loopback_profile(profile_path)
    if not valid:
        raise ValueError(
            f"Invalid Fast DDS loopback profile '{profile_path}':\n"
            + "\n".join(f"  - {e}" for e in errs)
        )

    env_map = get_loopback_env(profile_path)
    for k, v in env_map.items():
        existing = os.environ.get(k)
        if existing is not None and existing != v:
            sys.stderr.write(
                f"[fastdds_setup] Notice: Overriding conflicting inherited {k}='{existing}' with '{v}'\n"
            )
        os.environ[k] = v

    return env_map


if __name__ == "__main__":
    try:
        prof = resolve_loopback_profile()
        valid, errs = validate_loopback_profile(prof)
        if not valid:
            print(f"FAIL: Profile validation failed for {prof}:", file=sys.stderr)
            for err in errs:
                print(f"  - {err}", file=sys.stderr)
            sys.exit(1)
        print(f"PASS: Validated Fast DDS loopback profile at {prof}")
        for k, v in get_loopback_env(prof).items():
            print(f"{k}={v}")
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
