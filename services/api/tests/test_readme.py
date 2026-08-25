"""The counts printed on the front page, checked against the suite that produces them.

WHY THIS FILE EXISTS. A badge is an image. Nobody proofreads an image, and a test count on one
goes stale the moment a test is added — silently, on the most-read line of the repository. This
is not hypothetical: a sibling project carried a wrong `tests-603 passing` badge for weeks
precisely because it looked like infrastructure rather than prose.

So the number is parsed back out of the README and compared with the number of tests pytest
actually collected in this run. Add a test without touching the README and this fails.

WHAT IT CANNOT CHECK, stated so the guarantee is not overread: the TypeScript half. Vitest runs
in a different process and generates some of its cases in a loop, so counting them from here
would mean either shelling out to npm from a Python test or regex-counting `it(` and getting the
wrong answer. The README therefore states both halves separately, and this file verifies the
Python half and the arithmetic between them. The TypeScript number is checked by running
`npm test` and reading it — which is a weaker guarantee than the other half has, and saying so
is better than implying otherwise.
"""

from __future__ import annotations

import pathlib
import re

README = pathlib.Path(__file__).resolve().parents[3] / "README.md"

#: "[![tests](https://img.shields.io/badge/tests-198%20passing-22863a)](#tests)"
BADGE = re.compile(r"badge/tests-(\d+)%20passing")

#: "npm test                       # 47 tests — syncing and text editing"
NPM_CLAIM = re.compile(r"npm test\s+#\s*(\d+) tests")

#: "pytest                         # 151 tests — research, syncing, the API, the live call"
PYTEST_CLAIM = re.compile(r"pytest\s+#\s*(\d+) tests")

#: "198 tests in total."
TOTAL_CLAIM = re.compile(r"^(\d+) tests in total", re.MULTILINE)


def readme() -> str:
    assert README.exists(), f"cannot find the README at {README}"
    return README.read_text(encoding="utf-8")


def claim(pattern: re.Pattern[str], what: str) -> int:
    match = pattern.search(readme())
    assert match, f"the README no longer states {what}"
    return int(match.group(1))


class TestTheFrontPageCounts:
    def test_the_python_count_is_the_number_of_tests_that_ran(self, collected_test_count: int):
        published = claim(PYTEST_CLAIM, "how many tests `pytest` runs")
        assert published == collected_test_count, (
            f"the README says `pytest` runs {published} tests; this run collected "
            f"{collected_test_count}. Update the README — including the badge and the total."
        )

    def test_the_badge_matches_the_two_halves(self):
        assert claim(BADGE, "a tests badge") == claim(NPM_CLAIM, "an `npm test` count") + claim(
            PYTEST_CLAIM, "a `pytest` count"
        )

    def test_the_total_in_the_prose_matches_the_badge(self):
        """Two numbers for the same thing, thirty lines apart. One of them will be edited alone."""
        assert claim(TOTAL_CLAIM, "a total") == claim(BADGE, "a tests badge")
