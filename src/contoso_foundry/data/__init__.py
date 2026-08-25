"""The shared Contoso data spine.

Three layers, one set of canonical identifiers:

* **A — relational spine.** The nouns every agent needs to agree on: customers,
  employees, products, suppliers, locations, orders, invoices, support cases,
  travel bookings and field-service work orders.
* **B — domain data.** The things a single agent is expert in, keyed to layer A
  so a travel answer and a support answer are talking about the same person.
* **C — evaluation fixtures.** Golden scenarios, authored by hand, that pin the
  behaviour we expect from the agents built on top.

The dataset is generated, not committed. ``foundry data build`` rebuilds it from
``config/data-spine.yaml`` and ``data/seed/``; ``data/build.lock.json`` records
what a correct build looks like so CI can prove the answer has not drifted.
"""

from __future__ import annotations

from .model import SCHEMA, Column, Table, ddl, table_by_name

__all__ = ["SCHEMA", "Column", "Table", "ddl", "table_by_name"]
