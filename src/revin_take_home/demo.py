"""Run a local, synthetic caller-context demonstration: uv run python -m revin_take_home.demo."""

import asyncio
import json

from .caller_context import Candidate, LookupUnavailable, bootstrap_context, normalize_number

MAYA = Candidate(customer_id="demo_maya", first_name="Maya")
OMAR = Candidate(customer_id="demo_omar", first_name="Omar")
LEAH = Candidate(customer_id="demo_leah", first_name="Leah")


class DemoDirectory:
    """In-memory fixtures only. No real customer or membership claims."""

    def __init__(self, matched_caller_number: str | None = None) -> None:
        # Optional test-phone alias for Maya. Never store a real number in source.
        self.matched_caller_number = normalize_number(matched_caller_number)
        if matched_caller_number and self.matched_caller_number is None:
            raise ValueError("DEMO_CALLER_NUMBER must be a possible phone number")
        if self.matched_caller_number == "+12125550102":
            raise ValueError("DEMO_CALLER_NUMBER cannot replace the shared-number fixture")

    async def find_by_phone(self, number: str) -> tuple[Candidate, ...]:
        if self.matched_caller_number and number == self.matched_caller_number:
            return (MAYA,)
        return {
            "+12125550101": (MAYA,),
            "+12125550102": (OMAR, LEAH),
        }.get(number, ())


class UnavailableDirectory:
    async def find_by_phone(self, number: str) -> tuple[Candidate, ...]:
        raise LookupUnavailable("Simulated directory outage")


async def main() -> None:
    for label, number, directory in [
        ("Unique match", "(212) 555-0101", DemoDirectory()),
        ("Shared number", "+12125550102", DemoDirectory()),
        ("No match", "+12125550103", DemoDirectory()),
        ("Withheld number", None, DemoDirectory()),
        ("Lookup outage", "+12125550101", UnavailableDirectory()),
    ]:
        context = await bootstrap_context(
            "demo_" + label.lower().replace(" ", "_"), number, directory
        )
        print(label)
        print(json.dumps(context.for_agent(), indent=2))
        print()


if __name__ == "__main__":
    asyncio.run(main())
