# 입력 지문의 합성 회귀 테스트로, 승인·인증이나 서비스 정확도를 검증하지 않음.
# 실행: python3 -B -m unittest discover -s backend/tests -p 'test_input_binding.py' -v
# 원문·확인/삭제 상태·순서·버전 변경과 사전 키 순서의 불변성을 검사

from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.tutor import step_verifier as verifier


def pair():
    problem = {
        "schema_version": "verification-problem-v1", "id": "binding-test", "revision": "v1",
        "equation": "2*(x+1)=6", "variable": "x", "domain": "real", "confirmed": True,
        "profile": "linear-equation-v1",
    }
    work = {
        "schema_version": "verification-work-v1", "problem_id": problem["id"],
        "problem_revision": problem["revision"], "steps": [
            {"id": "s1", "text": "2*x=4", "state": "active", "confirmed": True},
            {"id": "s2", "text": "x=2", "state": "active", "confirmed": True},
        ],
    }
    return problem, work


class InputFingerprintTests(unittest.TestCase):
    def test_key_order_is_irrelevant_and_inputs_are_not_mutated(self):
        p, w = pair()
        snapshot = deepcopy((p, w))
        reordered_p = dict(reversed(list(p.items())))
        reordered_w = dict(reversed(list(w.items())))
        reordered_w["steps"] = [dict(reversed(list(step.items()))) for step in w["steps"]]
        digest = verifier.input_fingerprint(p, w)
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(digest, verifier.input_fingerprint(reordered_p, reordered_w))
        self.assertEqual((p, w), snapshot)

    def test_every_problem_field_is_bound(self):
        p, w = pair()
        digest = verifier.input_fingerprint(p, w)
        changes = {
            "id": "other", "revision": "v2", "equation": "2*(x+1)=8", "variable": "y",
            "domain": "complex", "confirmed": False, "profile": "other-profile",
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                changed = deepcopy(p)
                changed[field] = value
                self.assertNotEqual(digest, verifier.input_fingerprint(changed, w))

    def test_every_work_field_and_observed_order_is_bound(self):
        p, w = pair()
        digest = verifier.input_fingerprint(p, w)
        for field, value in (("problem_id", "other"), ("problem_revision", "v2")):
            with self.subTest(field=field):
                changed = deepcopy(w)
                changed[field] = value
                self.assertNotEqual(digest, verifier.input_fingerprint(p, changed))
        for field, value in (("id", "other-step"), ("text", "2*x=6"),
                             ("state", "deleted"), ("confirmed", False)):
            with self.subTest(field=field):
                changed = deepcopy(w)
                changed["steps"][0][field] = value
                self.assertNotEqual(digest, verifier.input_fingerprint(p, changed))
        for changed_steps in (list(reversed(w["steps"])), w["steps"][:1], []):
            with self.subTest(steps=changed_steps):
                changed = deepcopy(w)
                changed["steps"] = deepcopy(changed_steps)
                self.assertNotEqual(digest, verifier.input_fingerprint(p, changed))

    def test_raw_text_is_not_normalized_and_version_is_bound(self):
        p, w = pair()
        digest = verifier.input_fingerprint(p, w)
        changed = deepcopy(w)
        changed["steps"][0]["text"] = " 2*x=4 "
        self.assertNotEqual(digest, verifier.input_fingerprint(p, changed))
        with patch.object(verifier, "VERIFIER_VERSION", "linear-equation-future"):
            self.assertNotEqual(digest, verifier.input_fingerprint(p, w))

    def test_fingerprint_validates_contract_before_hashing(self):
        p, w = pair()
        p["confirmed"] = 1
        with self.assertRaises(ValueError):
            verifier.input_fingerprint(p, w)
        p, w = pair()
        w["steps"][1]["id"] = w["steps"][0]["id"]
        with self.assertRaises(ValueError):
            verifier.input_fingerprint(p, w)

    def test_every_verification_result_has_current_input_binding(self):
        cases = []
        p, w = pair(); cases.append((p, w))
        p, w = pair(); w["problem_revision"] = "stale"; cases.append((p, w))
        p, w = pair(); p["confirmed"] = False; cases.append((p, w))
        p, w = pair(); p["profile"] = "geometry-v1"; cases.append((p, w))
        p, w = pair(); p["equation"] = "x*x=4"; cases.append((p, w))
        p, w = pair(); p["equation"] = "x=x"; cases.append((p, w))
        p, w = pair(); w["steps"] = []; cases.append((p, w))
        for p, w in cases:
            with self.subTest(problem=p, work=w):
                result = verifier.verify(p, w)
                self.assertEqual(result["input_fingerprint"], verifier.input_fingerprint(p, w))
                self.assertEqual(result["verifier_version"], verifier.VERIFIER_VERSION)


if __name__ == "__main__":
    unittest.main()
