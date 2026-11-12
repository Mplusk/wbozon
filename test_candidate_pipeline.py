import copy
import unittest

from pipeline.candidate import Stage, advance, blockers


class CandidatePipelineTests(unittest.TestCase):
    def test_missing_idea_data_blocks_and_does_not_change_input(self):
        candidate = {"stage": "idea", "idea": {"license": "NC"}}
        original = copy.deepcopy(candidate)
        self.assertTrue(any("model_url" in e for e in blockers(candidate)))
        with self.assertRaises(ValueError):
            advance(candidate)
        self.assertEqual(candidate, original)

    def test_license_is_recorded_not_filtered(self):
        candidate = {"stage": "idea", "idea": {
            "model_url": "model", "author_url": "author", "license": "NC",
            "photo_url": "photo", "print_profile_source": "profile",
            "mass_g": 100, "print_hours": 2,
        }}
        self.assertEqual(advance(candidate)["stage"], Stage.DEMAND.value)
        self.assertEqual(candidate["stage"], Stage.IDEA.value)

    def test_demand_threshold_and_sources(self):
        candidate = {"stage": "demand", "demand": {"score": 59, "price_min": 300, "price_max": 600, "source": "report"}}
        self.assertTrue(any("ниже порога" in e for e in blockers(candidate)))
        candidate["demand"]["score"] = 60
        self.assertEqual(advance(candidate)["stage"], "preliminary_economics")
        candidate["demand"]["source"] = ""
        self.assertTrue(any("source" in e for e in blockers(candidate)))

    def test_preliminary_requires_screening_and_independent_review(self):
        candidate = {"stage": "preliminary_economics", "idea": {"mass_g": 300, "print_hours": 6},
                     "screening": {"packaging_liters": 5, "significant_postprocessing": False,
                                   "restricted_category": False, "third_party_brand": False, "evidence": "assessment"},
                     "preliminary_economics": {"calculation": {"passed": True, "evidence": "calculation.json"},
                                               "review": {"passed": True, "evidence": "review.md"}}}
        self.assertEqual(advance(candidate)["stage"], "sample_approval")
        candidate["screening"]["third_party_brand"] = True
        self.assertTrue(any("third_party_brand" in e for e in blockers(candidate)))
        candidate["screening"]["third_party_brand_owner_override"] = "owner decision"
        candidate["preliminary_economics"]["review"]["passed"] = False
        self.assertTrue(any("review" in e for e in blockers(candidate)))

    def test_three_owner_decisions_and_settled_rights(self):
        candidate = {"stage": "sample_approval", "approvals": {}}
        self.assertTrue(any("approvals.sample" in e for e in blockers(candidate)))
        candidate["approvals"]["sample"] = {"approved": True, "channel": "telegram", "evidence": "message:1"}
        self.assertEqual(advance(candidate)["stage"], "sample")
        candidate.update(stage="launch_approval", rights={"status": "in_progress"},
                         compliance={"required_document": "assessment:1"},
                         launch_plan={"first_batch_budget": 1000, "advertising_budget": 200})
        candidate["approvals"]["launch"] = {"approved": True, "channel": "telegram", "evidence": "message:2"}
        self.assertTrue(any("rights.status" in e for e in blockers(candidate)))
        candidate["rights"] = {"status": "settled", "owner_evidence": "message:rights"}
        self.assertEqual(advance(candidate)["stage"], "listing")
        candidate["stage"] = "publication_approval"
        candidate["listing"] = {"api_validation": {"passed": True, "evidence": "api response"},
                                "quality_review": {"passed": True, "evidence": "review"}}
        self.assertTrue(any("approvals.publication" in e for e in blockers(candidate)))
        candidate["approvals"]["publication"] = {"approved": True, "channel": "telegram", "evidence": "message:3"}
        self.assertEqual(advance(candidate)["stage"], "first_batch")
        candidate["rights"]["status"] = "unverified"
        self.assertTrue(any("rights.status" in e for e in blockers(candidate)))

    def test_sample_and_launch_need_real_evidence(self):
        candidate = {"stage": "sample", "sample": {"acceptable": True, "mass_g": 100,
                     "print_hours": 2, "defect_rate": 0, "evidence": "print measurement record"}}
        # Sample measurements are required, but producing images for a listing
        # does not require photos from the owner.
        self.assertEqual(advance(candidate)["stage"], "final_economics")
        candidate["sample"]["evidence"] = ""
        self.assertTrue(any("evidence" in e for e in blockers(candidate)))
        candidate["sample"]["evidence"] = "print measurement record"
        candidate["sample"]["acceptable"] = False
        self.assertTrue(any("acceptable" in e for e in blockers(candidate)))
        candidate["sample"]["acceptable"] = True
        del candidate["sample"]["mass_g"]
        self.assertTrue(any("mass_g" in e for e in blockers(candidate)))
        candidate["sample"]["mass_g"] = 100
        candidate["sample"]["defect_rate"] = 1
        self.assertTrue(any("defect_rate" in e for e in blockers(candidate)))
        launch = {"stage": "launch", "launch": {"sales_days": 13, "test_budget_exhausted": False, "evidence": "report"}}
        self.assertTrue(blockers(launch))
        launch["launch"]["test_budget_exhausted"] = True
        self.assertEqual(advance(launch)["stage"], "operations")


if __name__ == "__main__":
    unittest.main()
