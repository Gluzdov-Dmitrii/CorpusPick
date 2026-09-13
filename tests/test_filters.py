import unittest
from corpuspick.filters import column_value, matches


class FilterTests(unittest.TestCase):
    def test_word_filters_and_exclusions(self):
        value = '001_Отчёт по испытаниям насоса.docx'
        self.assertTrue(matches(value, {'include': 'отчет насос'}))
        self.assertFalse(matches(value, {'include': 'отчет насос', 'whole': True}))
        self.assertTrue(matches(value, {'include': 'отчет насоса', 'whole': True}))
        self.assertFalse(matches(value, {'include': 'отчет', 'exclude': 'испытания'}))
        self.assertTrue(matches(value, {'include': 'акт отчет', 'any': True}))
        self.assertFalse(matches(value, {'include': 'акт отчет'}))

    def test_column_scope_and_numeric_unknown(self):
        document = {'path': 'reports/letter.pdf', 'origin': 'Отчет/letter.pdf', 'size': 100}
        self.assertFalse(matches(column_value(document, 'name'), {'include': 'отчет'}))
        self.assertTrue(matches(column_value(document, 'origin'), {'include': 'отчет'}))
        self.assertTrue(matches(100, {'numeric': True, 'min': 100, 'max': 100}))
        self.assertTrue(matches(0, {'numeric': True, 'max': 0}))
        self.assertFalse(matches(None, {'numeric': True, 'min': 0}))
        self.assertFalse(matches(101, {'numeric': True, 'max': 100}))
