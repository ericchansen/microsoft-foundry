"""Framework-neutral tool contracts and the scoped data access behind them.

The contracts in ``config/toolbox`` describe *what* an agent may ask for. The
code here decides *whose* data answers the question, and it decides that from
the request's authenticated principal rather than from anything the model said.

Nothing in this package imports an agent framework. The contracts are plain
YAML with JSON Schema parameter blocks so the same definition can be published
to Foundry, exposed over MCP, or called directly in a test.
"""

from contoso_foundry.toolbox.contracts import (
    ContractError,
    ToolboxContract,
    ToolContract,
    load_contracts,
    validate_contracts,
)
from contoso_foundry.toolbox.identity import (
    IdentityResolver,
    Principal,
    RequestScope,
    UnknownPrincipalError,
)
from contoso_foundry.toolbox.repository import CohortTooSmallError
from contoso_foundry.toolbox.tools import Toolbox, ToolError

__all__ = [
    "CohortTooSmallError",
    "ContractError",
    "IdentityResolver",
    "Principal",
    "RequestScope",
    "ToolContract",
    "ToolError",
    "Toolbox",
    "ToolboxContract",
    "UnknownPrincipalError",
    "load_contracts",
    "validate_contracts",
]