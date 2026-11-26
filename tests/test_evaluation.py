import unittest

from evaluate_reading import select_cases
from live_novel import ARC_RULES, REACTION_RULES, story_errors


class EvaluationChecks(unittest.TestCase):
    def test_reject_empty_or_invalid_selection(self):
        for numbers, total in (([], 0), ([99999], 20), ([1, 0], 20)):
            with self.subTest(numbers=numbers, total=total):
                with self.assertRaises(ValueError):
                    select_cases(numbers, total)
        self.assertEqual(select_cases([], 3), [1, 2, 3])
        self.assertEqual(select_cases([3, 1, 3], 3), [1, 3])

    def test_reaction_evaluation_checks_attribution(self):
        for lead, should_pass in (
            ("达西的信证实威克姆说谎，伊丽莎白也因此反省了自己的判断。", False),
            ("达西的信把威克姆的说法逐条推翻，她开始反省自己的判断。", False),
            ("达西的信让她重新审视威克姆的说法，她开始反省自己的判断。", True),
            ("达西的信不能证明威克姆说谎，却让她开始反省自己的判断。", True),
        ):
            with self.subTest(lead=lead):
                answer = {
                    "status": "answer",
                    "prose": True,
                    "claims": [
                        {"text": lead},
                        {"text": "威克姆说达西没给他牧师职位，达西在信里说他放弃职位换了三千镑。"},
                    ],
                }
                errors = story_errors(answer, {16, 35, 36}, **REACTION_RULES)
                self.assertEqual(not errors, should_pass, errors)

    def test_novel_arc_explains_the_change_with_the_right_debt_owner(self):
        for lead, should_pass in (
            ("达西的信让她质疑威克姆，丽迪雅的婚事又让她对达西生出敬意。", True),
            ("读完达西的信，又得知他帮助了丽迪雅，她为自己以前对他的刻薄态度难过。", True),
            ("达西写信说明了威克姆的俸禄，后来还帮了丽迪雅。", False),
            ("达西来信解释了威克姆的事，丽迪雅后来嫁了人。", False),
        ):
            with self.subTest(lead=lead):
                answer = {
                    "status": "answer",
                    "prose": True,
                    "claims": [
                        {"text": lead},
                        {"text": "威克姆自称失去牧师职位。达西称他改要三千镑，后来达西还清威克姆的债务。"},
                    ],
                }
                errors = story_errors(answer, {16, 35, 36, 52}, **ARC_RULES)
                self.assertEqual(not errors, should_pass, errors)
        answer["claims"][0]["text"] = "达西的信让她质疑威克姆，丽迪雅的婚事又让她对达西生出敬意。"
        answer["claims"][1]["text"] = answer["claims"][1]["text"].replace("威克姆的债务", "丽迪雅的债务")
        self.assertIn("归因或确定程度与原文不符", story_errors(answer, {16, 35, 36, 52}, **ARC_RULES))
