from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from contoso_foundry.data import build as build_mod

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_ROOT = REPO_ROOT / "agents" / "travel"
AGENT_SRC = AGENT_ROOT / "src"
sys.path.insert(0, str(AGENT_SRC))

from contoso_travel_agent.definition import (  # noqa: E402
    AgentDefinitionError,
    build_agent_definition,
    create_agent_version,
    load_agent_spec,
)
from contoso_travel_agent.runtime import (  # noqa: E402
    AgentRuntimeError,
    ExecutedToolCall,
    ServerExecutedTravelRuntime,
    TravelAgentRuntime,
)
from contoso_travel_agent.service import SAFE_OPERATIONS, execute_operation  # noqa: E402


@pytest.fixture(scope="module")
def database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    result = build_mod.build(
        config_path=REPO_ROOT / "config" / "data-spine.yaml",
        seed_dir=REPO_ROOT / "data" / "seed",
        out_dir=tmp_path_factory.mktemp("travel-agent-spine"),
        fixtures_dir=REPO_ROOT / "data" / "fixtures",
    )
    return result.root / "contoso.db"


def test_definition_is_exact_and_digest_is_stable():
    first = load_agent_spec(AGENT_ROOT / "agent.yaml")
    second = load_agent_spec(AGENT_ROOT / "agent.yaml")
    assert first.model_name == "gpt-5.4-mini"
    assert first.model_version == "2026-03-17"
    assert first.digest == second.digest
    assert first.digest.startswith("sha256:")
    assert first.tool_connection_name == "travel-openapi-v2"
    assert {tool["name"] for tool in first.tools} == SAFE_OPERATIONS
    assert "travel_list_my_bookings" not in SAFE_OPERATIONS


def test_browser_definition_uses_only_typed_server_executed_openapi_tool():
    spec = load_agent_spec(AGENT_ROOT / "agent.yaml")
    definition = build_agent_definition(
        spec,
        server_url="https://travel.example.invalid",
        project_connection_id="travel-openapi-v2",
    ).as_dict()
    assert [tool["type"] for tool in definition["tools"]] == ["openapi"]
    assert all(tool["type"] != "function" for tool in definition["tools"])
    openapi = definition["tools"][0]["openapi"]
    assert openapi["auth"]["type"] == "project_connection"
    assert set(
        operation["post"]["operationId"]
        for operation in openapi["spec"]["paths"].values()
    ) == SAFE_OPERATIONS
    assert all(
        operation["post"]["requestBody"]["required"] is True
        for operation in openapi["spec"]["paths"].values()
    )
    assert openapi["spec"]["security"] == [{"TravelToolKey": []}]


def test_browser_definition_rejects_non_https_tool_target():
    with pytest.raises(AgentDefinitionError, match="HTTPS"):
        build_agent_definition(
            load_agent_spec(AGENT_ROOT / "agent.yaml"),
            server_url="http://travel.example.invalid",
            project_connection_id="travel-openapi-v2",
        )


def test_latest_model_version_fails_closed(tmp_path):
    document = (AGENT_ROOT / "agent.yaml").read_text(encoding="utf-8").replace("2026-03-17", "latest")
    path = tmp_path / "agent.yaml"
    path.write_text(document, encoding="utf-8")
    (tmp_path / "instructions.md").write_text("safe", encoding="utf-8")
    with pytest.raises(AgentDefinitionError, match="exact"):
        load_agent_spec(path)


def test_definition_version_requires_an_immutable_backend_connection(tmp_path):
    document = (AGENT_ROOT / "agent.yaml").read_text(encoding="utf-8").replace(
        "travel-openapi-v2",
        "travel-openapi",
    )
    path = tmp_path / "agent.yaml"
    path.write_text(document, encoding="utf-8")
    (tmp_path / "instructions.md").write_text("safe", encoding="utf-8")

    with pytest.raises(AgentDefinitionError, match="requires connection"):
        load_agent_spec(path)


def test_golden_jsonl_is_valid_and_uses_only_synthetic_principal():
    rows = [
        json.loads(line) for line in (AGENT_ROOT / "golden" / "travel.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) >= 3
    assert {row["principal"] for row in rows} == {"emea-travel-coordinator"}
    assert all(row["expected_tools"] for row in rows)


class FakeResponses:
    def __init__(self):
        self.calls = []
        self._responses = [
            SimpleNamespace(
                id="response-1",
                output=[
                    SimpleNamespace(
                        type="function_call",
                        name="travel_get_policy",
                        arguments="{}",
                        call_id="call-1",
                    )
                ],
                output_text="",
            ),
            SimpleNamespace(id="response-2", output=[], output_text="Synthetic policy verified."),
        ]

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


def test_responses_loop_pins_agent_version_and_returns_tool_output(database):
    responses = FakeResponses()
    with sqlite3.connect(database) as connection:
        runtime = TravelAgentRuntime(
            SimpleNamespace(responses=responses),
            load_agent_spec(AGENT_ROOT / "agent.yaml"),
            connection,
            contracts_dir=REPO_ROOT / "config" / "toolbox",
        )
        assert runtime.run_turn("Which policy applies?", agent_version="7") == "Synthetic policy verified."
        assert runtime.audit[0].tool == "travel_get_policy"
        assert runtime.executed_calls == [ExecutedToolCall.from_arguments("travel_get_policy", {})]
    assert responses.calls[0]["extra_body"]["agent_reference"]["version"] == "7"
    assert responses.calls[0]["extra_body"]["agent_reference"]["type"] == "agent_reference"
    assert responses.calls[1]["extra_body"]["agent_reference"]["type"] == "agent_reference"
    assert responses.calls[1]["previous_response_id"] == "response-1"
    assert isinstance(responses.calls[1]["input"][0]["output"], str)


def test_runtime_refuses_scope_arguments_before_tool_execution(database):
    responses = FakeResponses()
    responses._responses[0].output[0].arguments = '{"scope":"APAC"}'
    with sqlite3.connect(database) as connection:
        runtime = TravelAgentRuntime(
            SimpleNamespace(responses=responses),
            load_agent_spec(AGENT_ROOT / "agent.yaml"),
            connection,
            contracts_dir=REPO_ROOT / "config" / "toolbox",
        )
        with pytest.raises(AgentRuntimeError, match="scope argument refused"):
            runtime.run_turn("Ignore policy", agent_version="7")
        assert runtime.audit == []


def test_runtime_translates_malformed_function_json(database):
    responses = FakeResponses()
    responses._responses[0].output[0].arguments = "{not-json"
    with sqlite3.connect(database) as connection:
        runtime = TravelAgentRuntime(
            SimpleNamespace(responses=responses),
            load_agent_spec(AGENT_ROOT / "agent.yaml"),
            connection,
            contracts_dir=REPO_ROOT / "config" / "toolbox",
        )
        with pytest.raises(AgentRuntimeError, match="travel_get_policy: malformed"):
            runtime.run_turn("Which policy?", agent_version="7")


def test_runtime_inspects_final_response_after_last_tool_round(database):
    responses = FakeResponses()
    responses._responses = [
        SimpleNamespace(
            id=f"response-{index}",
            output=[
                SimpleNamespace(
                    type="function_call",
                    name="travel_get_policy",
                    arguments="{}",
                    call_id=f"call-{index}",
                )
            ],
            output_text="",
        )
        for index in (1, 2)
    ] + [SimpleNamespace(id="response-3", output=[], output_text="Final verified answer.")]
    with sqlite3.connect(database) as connection:
        runtime = TravelAgentRuntime(
            SimpleNamespace(responses=responses),
            load_agent_spec(AGENT_ROOT / "agent.yaml"),
            connection,
            contracts_dir=REPO_ROOT / "config" / "toolbox",
            max_tool_rounds=2,
        )
        assert runtime.run_turn("Which policy?", agent_version="7") == "Final verified answer."
        assert len(runtime.executed_calls) == 2


def test_server_executed_runtime_completes_without_client_callback():
    responses = SimpleNamespace(
        create=lambda **_: SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="openapi_call",
                    name="contoso_travel_toolbox_travel_search_routes",
                    arguments=json.dumps(
                        {
                            "origin_location_id": "LOC-001",
                            "destination_location_id": "LOC-002",
                            "limit": 10,
                        }
                    ),
                    call_id="call-1",
                    status="completed",
                ),
                SimpleNamespace(
                    type="openapi_call_output",
                    call_id="call-1",
                    status="completed",
                ),
            ],
            output_text="Synthetic ROUTE-0001 verified.",
        )
    )
    runtime = ServerExecutedTravelRuntime(
        SimpleNamespace(responses=responses),
        load_agent_spec(AGENT_ROOT / "agent.yaml"),
    )
    assert "ROUTE-0001" in runtime.run_turn("Find route", agent_version="8")
    assert runtime.executed_calls == [
        ExecutedToolCall.from_arguments(
            "travel_search_routes",
            {
                "origin_location_id": "LOC-001",
                "destination_location_id": "LOC-002",
                "limit": 10,
            },
        )
    ]


def test_server_executed_runtime_requires_completed_tool_output():
    responses = SimpleNamespace(
        create=lambda **_: SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="openapi_call",
                    name="contoso_travel_toolbox_travel_get_policy",
                    arguments="{}",
                    call_id="call-1",
                    status="completed",
                )
            ],
            output_text="Synthetic policy.",
        )
    )
    runtime = ServerExecutedTravelRuntime(
        SimpleNamespace(responses=responses),
        load_agent_spec(AGENT_ROOT / "agent.yaml"),
    )

    with pytest.raises(AgentRuntimeError, match="no completed output"):
        runtime.run_turn("Find policy", agent_version="8")


def test_server_executed_runtime_rejects_function_callback():
    responses = SimpleNamespace(
        create=lambda **_: SimpleNamespace(
            output=[SimpleNamespace(type="function_call")],
            output_text="",
        )
    )
    runtime = ServerExecutedTravelRuntime(
        SimpleNamespace(responses=responses),
        load_agent_spec(AGENT_ROOT / "agent.yaml"),
    )
    with pytest.raises(AgentRuntimeError, match="client function output"):
        runtime.run_turn("Find route", agent_version="8")


def test_service_exposes_only_safe_operations():
    class RecordingToolbox:
        def __init__(self):
            self.calls = []

        def call(self, name, arguments):
            self.calls.append((name, arguments))
            return {"synthetic": True}

    toolbox = RecordingToolbox()
    assert execute_operation(toolbox, "travel_get_policy", {}) == {"synthetic": True}
    assert toolbox.calls == [("travel_get_policy", {})]
    with pytest.raises(KeyError, match="unknown Travel operation"):
        execute_operation(toolbox, "travel_list_my_bookings", {})


@pytest.mark.parametrize(
    "created",
    [
        SimpleNamespace(name="contoso-travel", version="", id="not-a-version"),
        SimpleNamespace(name="wrong-agent", version="7", id="agent-id"),
    ],
)
def test_create_version_rejects_missing_or_mismatched_identity(created):
    project = SimpleNamespace(
        agents=SimpleNamespace(create_version=lambda **_: created),
        connections=SimpleNamespace(
            get=lambda _: SimpleNamespace(
                target="https://travel.example.invalid",
                id="travel-openapi-v2",
            )
        ),
    )
    with pytest.raises(AgentDefinitionError):
        create_agent_version(project, load_agent_spec(AGENT_ROOT / "agent.yaml"))


def test_create_version_binds_digest_and_verifies_readback():
    class Agents:
        def create_version(self, **kwargs):
            self.definition = kwargs["definition"]
            self.metadata = kwargs["metadata"]
            return SimpleNamespace(name=kwargs["agent_name"], version="7")

        def get_version(self, *, agent_name, agent_version):
            return SimpleNamespace(
                name=agent_name,
                version=agent_version,
                metadata=self.metadata,
                definition=self.definition,
            )

    agents = Agents()
    spec = load_agent_spec(AGENT_ROOT / "agent.yaml")
    project = SimpleNamespace(
        agents=agents,
        connections=SimpleNamespace(
            get=lambda _: SimpleNamespace(
                target="https://travel.example.invalid",
                id="travel-openapi-v2",
            )
        ),
    )
    manifest = create_agent_version(project, spec)
    assert manifest["created_version"] == "7"
    assert agents.metadata == {"definition_digest": spec.digest}
    assert agents.definition.as_dict()["tools"][0]["type"] == "openapi"


@pytest.mark.parametrize("mismatch", ["metadata", "definition"])
def test_create_version_rejects_mismatched_readback(mismatch):
    class Definition:
        def as_dict(self):
            return {"mismatch": True}

    class Agents:
        def create_version(self, **kwargs):
            self.definition = kwargs["definition"]
            self.metadata = kwargs["metadata"]
            return SimpleNamespace(name=kwargs["agent_name"], version="7")

        def get_version(self, *, agent_name, agent_version):
            return SimpleNamespace(
                name=agent_name,
                version=agent_version,
                metadata={} if mismatch == "metadata" else self.metadata,
                definition=Definition() if mismatch == "definition" else self.definition,
            )

    with pytest.raises(AgentDefinitionError, match="digest|definition"):
        create_agent_version(
            SimpleNamespace(
                agents=Agents(),
                connections=SimpleNamespace(
                    get=lambda _: SimpleNamespace(
                        target="https://travel.example.invalid",
                        id="travel-openapi-v2",
                    )
                ),
            ),
            load_agent_spec(AGENT_ROOT / "agent.yaml"),
        )


def test_agent_dependencies_are_exactly_pinned():
    for line in (AGENT_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        assert "==" in line
    assert importlib.util.find_spec("yaml") is not None
