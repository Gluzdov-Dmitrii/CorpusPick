"""Disposable LibreOffice profile for passive first-page rendering."""
from pathlib import Path
from xml.etree import ElementTree as ET

OFFICE_CONVERSION_TIMEOUT = 20
PREVIEW_TIMEOUT = 25
OFFICE_STATS_TIMEOUT = 45
OFFICE_MEMORY_LIMIT = 1536 * 1024 * 1024


def create_profile(profile: Path) -> None:
    user = profile / 'user'
    user.mkdir(parents=True)
    ns = 'http://openoffice.org/2001/registry'
    ET.register_namespace('oor', ns)
    root = ET.Element(f'{{{ns}}}items')
    settings = {
        '/org.openoffice.Office.Common/Security/Scripting': {
            'DisableMacrosExecution': True, 'DisableActiveContent': True,
            'MacroSecurityLevel': 3, 'BlockUntrustedRefererLinks': True,
        },
        '/org.openoffice.Office.Writer/Content/Update': {'Link': 2, 'Field': False, 'Chart': False},
        '/org.openoffice.Office.Calc/Content/Update': {'Link': 1},
        '/org.openoffice.Office.Common/Misc': {'UseOpenCL': False},
        '/org.openoffice.Office.Common/VCL': {'UseSkia': False, 'DisableOpenGL': True},
        '/org.openoffice.Office.Common/Save/Document': {'CreateBackup': False},
        '/org.openoffice.Office.Recovery/AutoSave': {'Enabled': False},
        '/org.openoffice.Office.Jobs/Jobs/org.openoffice.Office.Jobs:Job[\'UpdateCheck\']/Arguments': {
            'AutoCheckEnabled': False,
        },
    }
    for path, properties in settings.items():
        item = ET.SubElement(root, 'item', {f'{{{ns}}}path': path})
        for name, value in properties.items():
            prop = ET.SubElement(item, 'prop', {f'{{{ns}}}name': name, f'{{{ns}}}op': 'fuse'})
            ET.SubElement(prop, 'value').text = str(value).lower()
    ET.ElementTree(root).write(user / 'registrymodifications.xcu', encoding='utf-8', xml_declaration=True)


def pdf_filter(suffix: str) -> str:
    family = 'calc' if suffix in ('.xls', '.xlsx') else 'impress' if suffix in ('.ppt', '.pptx') else 'writer'
    return 'pdf:' + family + '_pdf_Export:{"PageRange":{"type":"string","value":"1"}}'
