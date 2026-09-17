import subprocess
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from corpuspick.office_profile import create_profile, pdf_filter, OFFICE_MEMORY_LIMIT
from corpuspick.preview import _render_office_with_libreoffice


def test_passive_profile_settings(tmp_path):
    profile = tmp_path / 'profile'
    create_profile(profile)
    ns = '{http://openoffice.org/2001/registry}'
    root = ET.parse(profile / 'user' / 'registrymodifications.xcu').getroot()
    values = {(item.get(ns + 'path'), prop.get(ns + 'name')): prop.findtext('value')
              for item in root for prop in item}
    security = '/org.openoffice.Office.Common/Security/Scripting'
    for name in ('DisableMacrosExecution', 'DisableActiveContent', 'BlockUntrustedRefererLinks'):
        assert values[security, name] == 'true'
    assert values['/org.openoffice.Office.Writer/Content/Update', 'Link'] == '2'
    assert values['/org.openoffice.Office.Calc/Content/Update', 'Link'] == '1'
    assert values['/org.openoffice.Office.Common/VCL', 'UseSkia'] == 'false'


def test_converter_gets_copy_and_resources_are_removed_after_timeout(tmp_path):
    original = tmp_path / 'original.doc'
    original.write_bytes(b'synthetic legacy document')
    seen = []
    def timeout(command, timeout, memory_limit):
        copy = Path(command[-1])
        assert copy != original
        assert copy.read_bytes() == original.read_bytes()
        assert memory_limit == OFFICE_MEMORY_LIMIT
        assert '--norestore' in command
        assert pdf_filter('.doc') in command
        seen.append(copy.parent)
        raise subprocess.TimeoutExpired('synthetic', timeout)
    with patch('corpuspick.preview._cached_image', return_value=None), \
            patch('corpuspick.preview._LIBREOFFICE_UNAVAILABLE', False), \
            patch('corpuspick.preview._LIBREOFFICE_FAILURES', 0), \
            patch('corpuspick.preview._find_soffice', return_value='synthetic-converter'), \
            patch('corpuspick.preview.run_owned_command', side_effect=timeout):
        assert _render_office_with_libreoffice(original) is None
    assert original.read_bytes() == b'synthetic legacy document'
    assert seen and not seen[0].exists()
