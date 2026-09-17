from unittest.mock import patch
from zipfile import ZipFile

from corpuspick.statistics import document_stats


def test_quick_docx_stats_does_not_start_office(tmp_path):
    path = tmp_path / 'synthetic.docx'
    with ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:tbl/><w:p/></w:body></w:document>')
    with patch('subprocess.run', side_effect=AssertionError('Quick stats must not start Office')):
        assert document_stats(path)['tables'] == 1
