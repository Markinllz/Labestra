import unittest

from dataset_tools.identifiers import normalize_external_id, validate_aliases
from labestra.schemas import Source


class IdentityTests(unittest.TestCase):
    def test_normalization(self):
        for value in (" DOI:10.18653/V1/ABC ", "https://doi.org/10.18653%2FV1%2FABC"):
            self.assertEqual(normalize_external_id(value), "10.18653/v1/abc")
        self.assertEqual(normalize_external_id("https://openalex.org/w123"), "W123")

    def test_invalid(self):
        for value in (None, "", "arxiv:1234", "W0", "10.1/no", "10.1234/a b"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_external_id(value)

    def test_cross_provider_collision(self):
        sources = [Source("a", "A", "ACL", "metadata", "10.1234/a"),
                   Source("b", "B", "OpenAlex", "metadata", "W12")]
        with self.assertRaises(ValueError):
            validate_aliases(sources, {"a": ["W12"]})


if __name__ == "__main__":
    unittest.main()
