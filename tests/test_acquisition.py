import unittest

from dataset_tools.acquire import ACLPage


class AcquisitionTests(unittest.TestCase):
    def test_abstract_is_not_page_chrome_or_heading(self):
        page = ACLPage()
        page.feed('<meta name="citation_title" content="A &amp; B">'
                  '<div class="card-body acl-abstract"><h5>Abstract</h5>'
                  '<span>Dense <b>and</b> sparse.</span></div><p>Other text</p>')
        self.assertEqual(page.metadata["citation_title"], ["A & B"])
        self.assertEqual("".join(page.abstract), "Dense and sparse.")

    def test_authors_are_not_overwritten(self):
        page = ACLPage()
        page.feed('<meta name="citation_author" content="A">'
                  '<meta name="citation_author" content="B">')
        self.assertEqual(page.metadata["citation_author"], ["A", "B"])
