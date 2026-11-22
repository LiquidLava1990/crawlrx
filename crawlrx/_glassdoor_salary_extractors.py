"""Salary-row extractors for Glassdoor.

Operates on Playwright element handles representing one salary row. Split
out of :mod:`._glassdoor_extractors` to keep that module under the code-quality
file-size ceiling.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from ._glassdoor_extractors import (
    _card_inner_text,
    parse_int_with_commas,
)


SALARY_TITLE_SELECTORS = (
    "[data-test='job-title'] a",
    ".jobTitle",
    "h3 a",
    "a[data-test='job-title']",
)

SALARY_MEDIAN_SELECTORS = (
    "[data-test='salary-median']",
    ".medianBasePay",
    ".salary-median",
)

SALARY_RANGE_SELECTORS = (
    "[data-test='salary-range']",
    ".salary-range",
    "[data-test='pay-range']",
)

SALARY_DATAPOINTS_SELECTORS = (
    "[data-test='salary-count']",
    ".salaryCount",
    "[data-test='data-points']",
)


async def extract_salary_title(row: "ElementHandle") -> Optional[str]:
    return await _card_inner_text(row, SALARY_TITLE_SELECTORS)


async def extract_salary_median(row: "ElementHandle") -> Optional[str]:
    return await _card_inner_text(row, SALARY_MEDIAN_SELECTORS)


async def extract_salary_range(row: "ElementHandle") -> Optional[str]:
    return await _card_inner_text(row, SALARY_RANGE_SELECTORS)


async def extract_salary_total_comp(row: "ElementHandle") -> Optional[str]:
    return await _card_inner_text(
        row, ("[data-test='total-comp']", ".totalComp")
    )


async def extract_salary_company(row: "ElementHandle") -> Optional[str]:
    return await _card_inner_text(
        row, ("[data-test='employer-name']", ".employerName")
    )


async def extract_salary_location(row: "ElementHandle") -> Optional[str]:
    return await _card_inner_text(row, ("[data-test='location']", ".location"))


async def extract_salary_data_points(row: "ElementHandle") -> Optional[int]:
    text = await _card_inner_text(row, SALARY_DATAPOINTS_SELECTORS)
    return parse_int_with_commas(text)
