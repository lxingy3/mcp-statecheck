"""Controlled defect scenarios used by the M1 and M2 acceptance checks."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FixtureDefinition:
    fixture_id: str
    transport: str
    description: str
    minimum_actions: tuple[str, ...]


FIXTURES = (
    FixtureDefinition(
        "http-error-as-timeout",
        "streamable-http",
        "An explicit HTTP 503 must remain distinct from a timeout.",
        ("connect", "initialize"),
    ),
    FixtureDefinition(
        "duplicate-concurrent-request-id",
        "stdio",
        "Two pending calls share one MCP request ID and receive ambiguous replies.",
        ("initialize", "initialized", "call-a", "call-b"),
    ),
    FixtureDefinition(
        "second-sse-resume-token-loss",
        "streamable-http",
        "The second SSE reconnect must send the newest event ID.",
        ("initialize", "open-sse", "resume-1", "resume-2"),
    ),
    FixtureDefinition(
        "request-before-initialized",
        "stdio",
        "A peer accepts tools/list before initialization completes.",
        ("initialize-pending", "tools-list"),
    ),
    FixtureDefinition(
        "late-response-after-cancellation",
        "stdio",
        "A cancelled call replies late without consuming a later call.",
        ("initialize", "initialized", "call-a", "cancel-a", "call-b"),
    ),
)


def fixture_by_id(fixture_id: str) -> FixtureDefinition:
    for fixture in FIXTURES:
        if fixture.fixture_id == fixture_id:
            return fixture
    raise KeyError(fixture_id)
