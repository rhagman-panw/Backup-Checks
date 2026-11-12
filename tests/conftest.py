"""Synthetic test fixtures. No real configuration data."""
from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

PANORAMA_XML = """<?xml version="1.0"?>
<config version="10.2.0">
  <mgt-config>
    <users>
      <entry name="admin">
        <phash>$5$abcdefgh$SECRETHASHVALUE1234567890</phash>
        <permissions><role-based><superuser>yes</superuser></role-based></permissions>
      </entry>
    </users>
  </mgt-config>
  <devices>
    <entry name="localhost.localdomain">
      <deviceconfig>
        <system>
          <hostname>pano-lab-01</hostname>
          <snmp-setting><access-setting><version><v2c><snmp-community-string>PanoCommunity</snmp-community-string></v2c></version></access-setting></snmp-setting>
        </system>
      </deviceconfig>
      <device-group>
        <entry name="DG-Branch">
          <address>
            <entry name="web-server-01">
              <ip-netmask>10.1.1.10/32</ip-netmask>
            </entry>
          </address>
        </entry>
      </device-group>
    </entry>
  </devices>
  <panorama/>
</config>
"""

FW1_XML = """<?xml version="1.0"?>
<config version="10.2.0">
  <mgt-config>
    <users>
      <entry name="admin">
        <phash>$5$fw1salt$FW1HASHVALUE</phash>
      </entry>
    </users>
  </mgt-config>
  <devices>
    <entry name="localhost.localdomain">
      <deviceconfig>
        <system>
          <hostname>fw-branch-01</hostname>
          <ip-address>192.0.2.10</ip-address>
        </system>
      </deviceconfig>
      <network>
        <ike>
          <gateway>
            <entry name="gw-hq">
              <authentication>
                <pre-shared-key>
                  <key>-AQ==TopSecretPSK123=</key>
                </pre-shared-key>
              </authentication>
              <peer-address><ip>203.0.113.5</ip></peer-address>
            </entry>
          </gateway>
        </ike>
      </network>
      <vsys>
        <entry name="vsys1">
          <address>
            <entry name="web-server-01">
              <ip-netmask>10.1.1.10/32</ip-netmask>
            </entry>
          </address>
          <server-profile>
            <ldap><entry name="corp"><bind-password>LdapBindPw!</bind-password></entry></ldap>
          </server-profile>
        </entry>
      </vsys>
    </entry>
  </devices>
</config>
"""

FW2_XML = """<?xml version="1.0"?>
<config version="10.2.0">
  <devices>
    <entry name="localhost.localdomain">
      <deviceconfig>
        <system>
          <hostname>fw-dc-02</hostname>
        </system>
      </deviceconfig>
      <vsys>
        <entry name="vsys1">
          <rulebase><security><rules>
            <entry name="allow-dns">
              <destination><member>10.1.1.10</member></destination>
            </entry>
          </rules></security></rulebase>
        </entry>
      </vsys>
    </entry>
  </devices>
</config>
"""

FW1_SERIAL = "013201001111"
FW2_SERIAL = "013201002222"


def make_tar(path: Path, members: dict[str, bytes], mode: str = "w:gz") -> Path:
    with tarfile.open(path, mode) as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


@pytest.fixture
def bundle_path(tmp_path: Path) -> Path:
    """Build a bundle like Panorama's: top-level Panorama config plus per-device files."""
    return make_tar(
        tmp_path / "bundle.tgz",
        {
            "running-config.xml": PANORAMA_XML.encode(),
            f"devices/{FW1_SERIAL}/running-config.xml": FW1_XML.encode(),
            f"devices/{FW2_SERIAL}/running-config.xml": FW2_XML.encode(),
            "binary.bin": b"\x00\x01\x0210.1.1.10",
        },
    )


@pytest.fixture
def nested_bundle_path(tmp_path: Path) -> Path:
    inner = make_tar(
        tmp_path / f"{FW1_SERIAL}.tgz", {"running-config.xml": FW1_XML.encode()}
    )
    return make_tar(
        tmp_path / "outer.tgz",
        {"running-config.xml": PANORAMA_XML.encode(), f"{FW1_SERIAL}.tgz": inner.read_bytes()},
    )
