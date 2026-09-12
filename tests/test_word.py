"""Opt-in local Word test; never touches user documents."""
import os
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile
from corpuspick.statistics import word_stats
from corpuspick.core import digest


@unittest.skipUnless(os.environ.get('CORPUSPICK_TEST_WORD') == '1', 'Optional local Word integration')
class WordIntegration(unittest.TestCase):
    def test_synthetic_document(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'synthetic.docx'
            with ZipFile(path, 'w') as z:
                z.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
                z.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
                z.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Synthetic report</w:t></w:r></w:p><w:tbl><w:tblPr/><w:tblGrid><w:gridCol w:w="4000"/></w:tblGrid><w:tr><w:tc><w:tcPr/><w:p><w:r><w:t>Synthetic table</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:p><w:r><w:br w:type="page"/><w:t>Appendix A</w:t></w:r></w:p><w:sectPr/></w:body></w:document>')
            original = digest(path)
            result = word_stats(path)
            self.assertEqual(result.get('pages'), 2, result)
            self.assertEqual(result['tables'], 1)
            self.assertEqual(result['appendices'], 1)
            self.assertEqual(result['figures'], 0)
            self.assertEqual(original, digest(path))
