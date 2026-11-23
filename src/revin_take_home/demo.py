"""Run a local, synthetic caller-context demonstration: uv run python -m revin_take_home.demo."""

import asyncio
import json

from .caller_context import Candidate, LookupUnavailable, bootstrap_context

MAYA = Candidate(customer_id="demo_maya", first_name="Maya")
OMAR = Candidate(customer_id="demo_omar", first_name="Omar")
LEAH = Candidate(customer_id="demo_leah", first_name="Leah")


class DemoDirectory:
    """In-memory fixtures only. No real customer or membership claims."""

    async def find_by_phone(self, number: str) -> tuple[Candidate, ...]:
        """Return the fixture candidates for a demo phone number."""
        return {
            "+12125550101": (MAYA,),
            "+12125550102": (OMAR, LEAH),
        }.get(number, ())


class UnavailableDirectory:
    async def find_by_phone(self, number: str) -> tuple[Candidate, ...]:
        """Simulate a directory outage for every lookup."""
        raise LookupUnavailable("Simulated directory outage")


async def main() -> None:
    """Print caller context for the built-in demo numbers and failure cases."""
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
