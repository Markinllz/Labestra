from copy import deepcopy
import unittest

from dataset_tools.review import agreement, packet_digest


class ReviewTests(unittest.TestCase):
    def setUp(self):
        objects = [{"case_id": "c", "object_id": str(i), "kind": "entailment", "label_options": ["supports", "contradicts", "uncertain"]} for i in range(4)]
        self.packet = {"objects": objects, "packet_sha256": packet_digest(objects), "rubric_version": "1"}
        self.first, self.second = [], []
        for author, target, labels in (("human1", self.first, ["supports", "supports", "contradicts", "contradicts"]),
                                       ("human2", self.second, ["supports", "contradicts", "supports", "contradicts"])):
            for obj, label in zip(objects, labels):
                target.append({"case_id": "c", "object_id": obj["object_id"], "kind": "entailment", "label": label,
                               "annotator_id": author, "rationale": "synthetic test only", "rubric_version": "1",
                               "packet_sha256": self.packet["packet_sha256"]})

    def test_known_kappa(self):
        report = agreement(self.packet, self.first, self.second)["by_kind"]["entailment"]
        self.assertEqual((report["matched"], report["total"], report["cohen_kappa"]), (2, 4, 0.0))
        self.assertEqual(len(report["disagreements"]), 2)

    def test_constant_labels_are_not_perfect_kappa(self):
        for row in self.first + self.second:
            row["label"] = "supports"
        report = agreement(self.packet, self.first, self.second)["by_kind"]["entailment"]
        self.assertEqual(report["agreement"], 1)
        self.assertIsNone(report["cohen_kappa"])

    def test_unfilled_and_misaligned_labels_rejected(self):
        for change in ({"label": None}, {"object_id": "wrong"}, {"rubric_version": "2"}, {"rationale": ""}, {"packet_sha256": "wrong"}):
            with self.subTest(change=change):
                second = deepcopy(self.second)
                second[0].update(change)
                with self.assertRaises(ValueError):
                    agreement(self.packet, self.first, second)
        with self.assertRaises(ValueError):
            agreement(self.packet, self.first, self.second[:-1])

    def test_same_person_cannot_count_as_independent(self):
        for row in self.second:
            row["annotator_id"] = "human1"
        with self.assertRaisesRegex(ValueError, "independent"):
            agreement(self.packet, self.first, self.second)

    def test_modified_packet_rejected(self):
        self.packet["objects"][0]["claim"] = "changed"
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            agreement(self.packet, self.first, self.second)
