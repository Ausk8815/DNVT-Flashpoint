"""
Configuration loader for DNVT SIP bridge.
Reads sip_extensions.ini and returns structured config.
"""

import configparser
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LineConfig:
    enabled: bool = False
    sip_server: str = ""
    sip_port: int = 5060
    transport: str = "udp"
    username: str = ""
    password: str = ""
    display_name: str = ""
    extension: str = ""


@dataclass
class GeneralConfig:
    dial_timeout: float = 4.0
    dial_terminator: str = "#"
    default_route: str = "sip"


@dataclass
class AppConfig:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    lines: list = field(default_factory=lambda: [LineConfig() for _ in range(4)])


def load_config(path: str = None) -> AppConfig:
    """
    Load configuration from sip_extensions.ini.

    Args:
        path: path to INI file (defaults to sip_extensions.ini in script dir)

    Returns:
        AppConfig with general settings and 4 LineConfig entries
    """
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sip_extensions.ini")

    cfg = AppConfig()

    if not os.path.exists(path):
        print(f"[config] No config file at {path} — using defaults (SIP disabled)")
        return cfg

    parser = configparser.ConfigParser()
    parser.read(path)

    # General section
    if parser.has_section("general"):
        g = parser["general"]
        cfg.general.dial_timeout = g.getfloat("dial_timeout", 4.0)
        cfg.general.dial_terminator = g.get("dial_terminator", "#")
        cfg.general.default_route = g.get("default_route", "sip")

    # Line sections
    for i in range(4):
        section = f"line{i+1}"
        if not parser.has_section(section):
            continue
        s = parser[section]
        lc = LineConfig(
            enabled=s.getboolean("enabled", False),
            sip_server=s.get("sip_server", ""),
            sip_port=s.getint("sip_port", 5060),
            transport=s.get("transport", "udp"),
            username=s.get("username", ""),
            password=s.get("password", ""),
            display_name=s.get("display_name", f"DNVT Line {i+1}"),
            extension=s.get("extension", ""),
        )
        cfg.lines[i] = lc

    enabled = [i+1 for i, lc in enumerate(cfg.lines) if lc.enabled]
    print(f"[config] Loaded {path} — enabled lines: {enabled or 'none'}")
    return cfg
