"""RouterOS Web Console bridge with live passthrough authentication and execution engine."""

from __future__ import annotations

import logging
import secrets
import shlex
import time
from dataclasses import dataclass
from typing import Any

import routeros_api
from routeros_api.exceptions import (
    RouterOsApiCommunicationError,
    RouterOsApiConnectionError,
    RouterOsApiError,
)

logger = logging.getLogger(__name__)

CONSOLE_SESSION_TIMEOUT_SECONDS = 300  # 5 minutes inactivity timeout


@dataclass
class ConsoleSession:
    """In-memory active console session holding authenticated credentials."""

    token: str
    username: str
    password: str
    host: str
    port: int
    identity: str
    created_at: float
    last_active: float


class ConsoleSessionManager:
    """Manages ephemeral in-memory console sessions with strict anti-linger security."""

    def __init__(self) -> None:
        self._sessions: dict[str, ConsoleSession] = {}

    def authenticate(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
    ) -> tuple[bool, str, str]:
        """Validate credentials live against MikroTik RouterOS API.

        Returns:
            (success: bool, token_or_error: str, identity: str)
        """
        self.cleanup_expired()

        if not username.strip():
            return False, "Username cannot be empty", ""

        try:
            pool = routeros_api.RouterOsApiPool(
                host=host,
                port=port,
                username=username,
                password=password,
                use_ssl=False,
                plaintext_login=True,
            )
            client = pool.get_api()
            # Query router identity
            identity = "MikroTik"
            try:
                ident_res = client.get_resource("/system/identity").get()
                if ident_res and isinstance(ident_res, list) and "name" in ident_res[0]:
                    identity = ident_res[0]["name"]
            except Exception as e:
                logger.warning("Could not fetch identity: %s", e)
            finally:
                pool.disconnect()

            token = secrets.token_urlsafe(32)
            now = time.time()
            session = ConsoleSession(
                token=token,
                username=username,
                password=password,
                host=host,
                port=port,
                identity=identity,
                created_at=now,
                last_active=now,
            )
            self._sessions[token] = session
            logger.info("Console session established for user '%s' on %s:%d", username, host, port)
            return True, token, identity

        except (RouterOsApiConnectionError, TimeoutError) as e:
            logger.warning("Console auth connection failure: %s", e)
            return False, "Connection to MikroTik API timed out or refused", ""
        except (RouterOsApiCommunicationError, RouterOsApiError) as e:
            logger.warning("Console auth communication failure: %s", e)
            return False, "Invalid username or password", ""
        except Exception as e:
            logger.error("Unexpected console auth error: %s", e)
            return False, f"Authentication error: {e}", ""

    def get_session(self, token: str | None) -> ConsoleSession | None:
        """Retrieve valid session if present and not expired; updates last_active."""
        if not token:
            return None

        session = self._sessions.get(token)
        if not session:
            return None

        now = time.time()
        if (now - session.last_active) > CONSOLE_SESSION_TIMEOUT_SECONDS:
            logger.info("Console session for '%s' expired due to inactivity", session.username)
            self._sessions.pop(token, None)
            return None

        session.last_active = now
        return session

    def terminate(self, token: str | None) -> bool:
        """Immediately destroy active console session and wipe credentials from memory."""
        if token and token in self._sessions:
            s = self._sessions.pop(token)
            logger.info("Console session for '%s' terminated", s.username)
            return True
        return False

    def cleanup_expired(self) -> int:
        """Evict all idle sessions beyond timeout."""
        now = time.time()
        expired = [
            tok
            for tok, s in self._sessions.items()
            if (now - s.last_active) > CONSOLE_SESSION_TIMEOUT_SECONDS
        ]
        for tok in expired:
            self._sessions.pop(tok, None)
        return len(expired)


# Global singleton instance
console_manager = ConsoleSessionManager()


def format_tabular_output(items: list[dict[str, Any]]) -> str:
    """Format a list of dictionary records into clean aligned monospace text."""
    if not items:
        return "(no items found)"

    # If single item with many fields (e.g. /system resource print or routerboard print),
    # format as vertical key-value pairs matching native Winbox CLI output style
    if len(items) == 1 and len(items[0]) > 5:
        data = items[0]
        filtered = {k: v for k, v in data.items() if k not in (".id", "invalid")}
        return format_key_value_output(filtered)

    # Determine columns in preferred order
    # Priority columns first, then others
    keys_order = [
        "id",
        "flags",
        "name",
        "address",
        "network",
        "interface",
        "type",
        "mtu",
        "running",
        "disabled",
        "comment",
        "status",
        "uptime",
        "rx_bytes",
        "tx_bytes",
        "seq",
        "host",
        "size",
        "ttl",
        "time",
    ]

    all_keys = list(items[0].keys())
    # Sort keys by preferred order or alphabetical
    def sort_key(k: str) -> int:
        return keys_order.index(k) if k in keys_order else 100

    cols = sorted(all_keys, key=sort_key)
    # Hide noisy internal fields unless they are the only ones
    cols = [c for c in cols if c not in (".id", "invalid")] or all_keys

    # Calculate column widths
    widths: dict[str, int] = {}
    for c in cols:
        header_len = len(c)
        max_val_len = max(len(str(item.get(c, ""))) for item in items)
        widths[c] = max(header_len, min(max_val_len, 40))

    # Build Header
    header_line = " ".join(f"{c.upper():<{widths[c]}}" for c in cols)
    separator = " ".join("-" * widths[c] for c in cols)

    lines = [header_line, separator]
    for item in items:
        row_parts = []
        for c in cols:
            val_str = str(item.get(c, ""))
            if len(val_str) > widths[c]:
                val_str = val_str[: widths[c] - 2] + ".."
            row_parts.append(f"{val_str:<{widths[c]}}")
        lines.append(" ".join(row_parts))

    return "\n".join(lines)


def format_key_value_output(data: dict[str, Any]) -> str:
    """Format single dictionary object as key-value pairs."""
    max_k = max(len(str(k)) for k in data) if data else 10
    lines = [f"{str(k):>{max_k}}: {str(v)}" for k, v in data.items()]
    return "\n".join(lines)


# Comprehensive RouterOS v6 Path Hierarchy for RB960PGS
ROUTEROS_V6_PATH_TREE: dict[str, Any] = {
    "interface": {
        "bridge": {"port": {}, "vlan": {}, "filter": {}, "nat": {}, "settings": {}},
        "bonding": {},
        "ethernet": {"switch": {}},
        "vlan": {},
        "list": {"member": {}},
        "gre": {},
        "eoip": {},
        "ipip": {},
        "sstp-client": {},
        "sstp-server": {},
        "pptp-client": {},
        "l2tp-client": {},
        "ovpn-client": {},
    },
    "ip": {
        "address": {},
        "arp": {},
        "accounting": {},
        "cloud": {},
        "dhcp-client": {},
        "dhcp-server": {
            "network": {},
            "lease": {},
            "option": {},
            "alert": {},
            "config": {},
        },
        "dhcp-relay": {},
        "dns": {"static": {}, "cache": {}},
        "firewall": {
            "filter": {},
            "nat": {},
            "mangle": {},
            "raw": {},
            "address-list": {},
            "connection": {},
            "service-port": {},
        },
        "hotspot": {
            "user": {"profile": {}},
            "profile": {},
            "active": {},
            "host": {},
            "ip-binding": {},
        },
        "ipsec": {
            "peer": {},
            "identity": {},
            "profile": {},
            "policy": {},
            "proposal": {},
        },
        "neighbor": {"discovery-settings": {}},
        "pool": {},
        "proxy": {"access": {}, "cache": {}},
        "route": {"rule": {}},
        "service": {},
        "settings": {},
        "smb": {"shares": {}, "users": {}},
        "socks": {},
        "traffic-flow": {},
        "upnp": {},
    },
    "system": {
        "backup": {},
        "clock": {"manual": {}},
        "health": {},
        "history": {},
        "identity": {},
        "license": {},
        "logging": {"action": {}, "rule": {}},
        "note": {},
        "ntp": {"client": {}, "server": {}},
        "package": {"update": {}},
        "reboot": {},
        "reset-configuration": {},
        "resource": {},
        "routerboard": {},
        "scheduler": {},
        "script": {},
        "shutdown": {},
    },
    "user": {"group": {}, "active": {}, "ssh-keys": {}},
    "routing": {
        "bgp": {"peer": {}, "instance": {}},
        "ospf": {"area": {}, "instance": {}, "interface": {}, "neighbor": {}, "network": {}},
        "filter": {},
        "route": {},
    },
    "queue": {
        "simple": {},
        "tree": {},
        "type": {},
        "interface": {},
    },
    "tool": {
        "ping": {},
        "traceroute": {},
        "bandwidth-test": {},
        "profile": {},
        "torch": {},
        "mac-scan": {},
        "ip-scan": {},
        "e-mail": {},
        "netwatch": {},
        "fetch": {},
    },
    "log": {},
    "export": {},
    "ping": {},
}

PATH_ABBREVIATIONS: dict[str, str] = {
    "add": "address",
    "addr": "address",
    "int": "interface",
    "inter": "interface",
    "sys": "system",
    "res": "resource",
    "rb": "routerboard",
    "router": "routerboard",
    "fi": "firewall",
    "fire": "firewall",
    "fil": "filter",
    "filt": "filter",
    "dhcp": "dhcp-server",
    "dhcp-s": "dhcp-server",
    "net": "network",
    "eth": "ethernet",
    "ether": "ethernet",
    "conn": "connection",
    "addr-list": "address-list",
    "usr": "user",
    "u": "user",
}

VALID_VERBS = [
    "print",
    "get",
    "add",
    "set",
    "remove",
    "enable",
    "disable",
    "comment",
    "export",
    "reset",
]

VERB_ABBREVIATIONS: dict[str, str] = {
    "pr": "print",
    "pri": "print",
    "prin": "print",
    "print": "print",
    "p": "print",
    "rem": "remove",
    "en": "enable",
    "dis": "disable",
    "exp": "export",
    "comm": "comment",
}


def resolve_command_tokens(tokens: list[str]) -> tuple[str, str, dict[str, str]]:
    """Resolve abbreviated and raw tokens into normalized RouterOS path, verb, and arguments."""
    curr_node: dict[str, Any] = ROUTEROS_V6_PATH_TREE
    path_parts: list[str] = []
    idx = 0
    verb = "print"
    args_dict: dict[str, str] = {}

    while idx < len(tokens):
        token = tokens[idx].strip("/").lower()
        if not token:
            idx += 1
            continue

        if "=" in token:
            break

        # Check explicit path abbreviation (e.g. 'add' -> 'address', 'sys' -> 'system')
        expanded = PATH_ABBREVIATIONS.get(token, token)
        if expanded in curr_node:
            path_parts.append(expanded)
            curr_node = curr_node[expanded]
            idx += 1
            continue

        # Check prefix match in path tree
        prefix_matches = [k for k in curr_node if k.startswith(token)]
        if len(prefix_matches) == 1:
            matched_key = prefix_matches[0]
            path_parts.append(matched_key)
            curr_node = curr_node[matched_key]
            idx += 1
            continue

        # Check if token matches an action verb
        v_expanded = VERB_ABBREVIATIONS.get(token, token)
        if v_expanded in VALID_VERBS:
            verb = v_expanded
            idx += 1
            break

        prefix_verbs = [v for v in VALID_VERBS if v.startswith(token)]
        if len(prefix_verbs) == 1:
            verb = prefix_verbs[0]
            idx += 1
            break

        # If not matched as path or verb, break to argument parsing
        break

    # Parse remaining arguments
    while idx < len(tokens):
        arg = tokens[idx]
        if arg in ("where", "detail", "brief"):
            idx += 1
            continue
        if "=" in arg:
            k, v = arg.split("=", 1)
            args_dict[k.strip()] = v.strip()
        else:
            if "id" not in args_dict and "numbers" not in args_dict:
                val = arg.strip()
                if val.startswith("*"):
                    args_dict["id"] = val
                else:
                    args_dict["numbers"] = val
        idx += 1

    resource_path = "/" + "/".join(path_parts) if path_parts else "/"
    return resource_path, verb, args_dict


def execute_routeros_command(
    session: ConsoleSession,
    raw_cmd: str,
) -> tuple[bool, str]:
    """Parse and execute a RouterOS command string through live API connection.

    Returns:
        (success: bool, output_formatted: str)
    """
    cmd = raw_cmd.strip()
    if not cmd:
        return True, ""

    if cmd.startswith("#"):
        return True, ""

    if cmd in ("quit", "exit", "/quit", "/exit"):
        return True, "Connection closed."

    try:
        tokens = shlex.split(cmd)
    except Exception as e:
        return False, f"Syntax error: {e}"

    if not tokens:
        return True, ""

    pool = routeros_api.RouterOsApiPool(
        host=session.host,
        port=session.port,
        username=session.username,
        password=session.password,
        use_ssl=False,
        plaintext_login=True,
    )

    try:
        client = pool.get_api()

        # Handle ping command (/ping or /tool ping)
        t0 = tokens[0].strip("/").lower()
        t1 = tokens[1].strip("/").lower() if len(tokens) > 1 else ""
        is_ping = t0 == "ping" or (t0 == "tool" and t1 == "ping")
        if is_ping:
            args: dict[str, str] = {}
            # Check arguments
            ping_tokens = tokens[2:] if (t0 == "tool" and t1 == "ping") else tokens[1:]
            for t in ping_tokens:
                if "=" in t:
                    k, v = t.split("=", 1)
                    args[k.strip()] = v.strip()
                elif "address" not in args:
                    args["address"] = t.strip()
            if "address" not in args:
                return False, "Usage: /ping <address> [count=N] [interval=N]"
            if "count" not in args:
                args["count"] = "4"

            res = client.get_resource("/").call("ping", args)
            return True, format_tabular_output(res)

        # Handle system reboot
        if t0 == "reboot" or (t0 == "system" and t1 == "reboot"):
            client.get_resource("/system").call("reboot")
            return True, "Rebooting system..."

        # Parse command tokens into resource path, action verb, and arguments
        # Handles shortcuts like 'ip add pr', 'sys res pr', 'int pr', etc.
        resource_path, action_verb, args_dict = resolve_command_tokens(tokens)

        # Execute according to action verb
        if action_verb in ("print", "get"):
            # Query resource
            resource = client.get_resource(resource_path)
            res = resource.get(**args_dict)
            if isinstance(res, list):
                return True, format_tabular_output(res)
            elif isinstance(res, dict):
                return True, format_key_value_output(res)
            return True, str(res)

        elif action_verb == "add":
            resource = client.get_resource(resource_path)
            resource.add(**args_dict)
            return True, f"Item added successfully to {resource_path}"

        elif action_verb == "set":
            resource = client.get_resource(resource_path)
            resource.set(**args_dict)
            return True, f"Item updated successfully in {resource_path}"

        elif action_verb == "remove":
            resource = client.get_resource(resource_path)
            resource.remove(**args_dict)
            return True, f"Item removed from {resource_path}"

        elif action_verb == "enable":
            resource = client.get_resource(resource_path)
            resource.call("enable", args_dict)
            return True, f"Item enabled in {resource_path}"

        elif action_verb == "disable":
            resource = client.get_resource(resource_path)
            resource.call("disable", args_dict)
            return True, f"Item disabled in {resource_path}"

        elif action_verb == "comment":
            resource = client.get_resource(resource_path)
            resource.call("comment", args_dict)
            return True, f"Comment applied in {resource_path}"

        else:
            return False, f"Unsupported action '{action_verb}' on '{resource_path}'"

    except (RouterOsApiCommunicationError, RouterOsApiError) as e:
        err_msg = str(e)
        return False, f"failure: {err_msg}"
    except (RouterOsApiConnectionError, TimeoutError):
        return False, "Error: Lost connection to MikroTik RouterOS"
    except Exception as e:
        return False, f"Execution error: {e}"
    finally:
        pool.disconnect()
