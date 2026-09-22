# Twenty-minute agent operations demo

**Audience:** enterprise platform owners and the engineers who operate agents.

**Story:** Contoso runs agents on different frameworks and hosts. A travel
request exposes a quality problem. We diagnose it, distinguish visibility from
enforcement, and compare a correction before changing the accepted version.

This is not a tour of every resource. Keep infrastructure, optional platforms,
and deployment commands outside the twenty-minute presentation.

## Prepare before the audience arrives

Use the authenticated demo Azure context, the current repository checkout, and
the dependencies in the [installation guide](operations/verification.md).
All mutable resources must remain in the resource group named in
`config/boundary.yaml`. Never substitute another group's resources or identities.

```powershell
$env:PYTHONPATH = "src;agents\travel\src"
foundry boundary --enable-module optional-control-plane
foundry gateway verify
foundry costs --enable-module approvals
```

The optional-module flags account for the deployed SRE/Approvals inventory and
Approvals cost assumptions; they do not start those agents. If those modules
are absent in your installation, use its actual enabled configuration. Stop on
a failed gate. A working chat does not establish that its configured content
protections are intact. See [model governance](platform/model-governance.md).
Historical traces and evaluation results can still be inspected read-only to
diagnose a failed gate; they do not certify current readiness. If another
controller is rewriting a policy, resolve the baseline with its owner rather
than repeatedly overwriting it or weakening the check.

Generate fresh evidence with the bounded rehearsal commands:

```powershell
python -m contoso_foundry.demo gateway --run --enable-module optional-control-plane
python -m contoso_foundry.demo field --run --enable-module optional-control-plane
```

Gateway makes one unauthenticated and one authenticated model request. Its
existing subscription credential stays in process memory. It neither rotates
that credential nor changes quotas. Field temporarily scales the owned internal
Container App from zero to one, executes the work-order scenario, restores zero
in `finally`, and checks fresh attributed telemetry. Neither command registers
an agent or promotes a version. Outputs are recorded incrementally in a new,
ignored `internal/demo/` directory.

Do not rerun Field while another deployment or rehearsal is active. If a process
is forcibly terminated before restoration, inspect `field-restoration.json`
privately and follow the [Field recovery procedure](agents/field.md#presenter-runbook).
Do not make ingress public to work around scale-to-zero.
If execution completed but restoration or ingestion exceeded its wait, first
confirm the owned app's minimum remains zero, then finish the read-only checks
without another model call:

```powershell
python -m contoso_foundry.demo field --resume `
  --output "internal\demo\<the recorded Field run directory>" `
  --enable-module optional-control-plane
```

Open these tabs before presenting:

| Tab | Where to go |
| --- | --- |
| Estate | Azure portal > `contoso-agents-insights` > **Agents (Preview)** |
| Travel | Foundry > `travel` > **Build > Agents > contoso-travel > Traces**, with the exact baseline response open |
| External trace | Application Insights > **Agents (Preview)** > `contoso-field` > **View Traces with Agent Runs** > matching transaction |
| Enforcement | Azure portal > `contoso-agents-logs` > **Logs** |
| Improvement | Foundry > `travel` > **Evaluations > Compare**, with both intended runs and the correct baseline selected |

The [Azure Monitor agent view](https://learn.microsoft.com/azure/azure-monitor/app/agents-view)
is also reachable from Travel's **Monitor > Open in Azure Monitor** button.
Choose a time range that actually contains the prepared runs; wait for ingestion
before presenting. Record the UTC interval beside each private response or
correlation ID. Relative windows move: yesterday's rehearsal can disappear from
**Last 24 hours** without any telemetry being lost. Use an explicit interval for
historical evidence and say when it executed. See
[Log Analytics scope and time range](https://learn.microsoft.com/azure/azure-monitor/logs/scope).
The [external-agent guide](https://learn.microsoft.com/azure/foundry/agents/how-to/register-external-agent#verify-traces-in-the-foundry-portal)
explains that telemetry is not instantaneous.

> **Privacy:** full URLs, tenant/resource identifiers, credentials, and raw
> execution IDs remain in the authenticated environment and ignored evidence.
> Do not publish uncropped screenshots or copy those values into slides,
> GitHub comments, or this site.

## 0-3 minutes: see the estate

Say:

> "These agents do different jobs and do not all run in Foundry. The common
> contract is operational evidence, not a requirement to use one framework."

In **Application Insights > Agents (Preview)**, select an appropriate time range
and **All agents**. Identify `contoso-travel` and `contoso-field`. Support and
Research may also appear; do not turn this into four separate agent demos.
Choose **Only main agents** when distinguishing business agents from internal
graph nodes.

Show real runs, token usage, and errors, then explain one visible change in the
time series. Do not equate span counts with conversations or an HTTP-completed
response with a successful business task.

The fictional workload produces real measurements. Say that once; do not make
every business question contain the word "synthetic."

!!! note "Two different monitoring surfaces"
    Foundry **Operate > Overview** and Application Insights **Agents (Preview)**
    are not interchangeable. If Operate's aggregate metrics are unavailable,
    do not imply that the empty screen is populated or that it represents zero
    usage. Use the native Azure Monitor experience above for the estate story,
    and Foundry's agent-specific Monitor/Traces views for investigation.

Microsoft documents the [cross-framework agent dashboard and trace drill-down](https://learn.microsoft.com/azure/azure-monitor/app/agents-view)
and the distinct [Foundry fleet monitoring surface](https://learn.microsoft.com/azure/foundry/control-plane/monitoring-across-fleet).

## 3-8 minutes: diagnose a business failure

<a id="primary-demo-part-1-contoso-travel-in-ten-minutes"></a>
<a id="observability-walkthrough"></a>

Introduce the business task, not the internal identifiers:

> "I need to travel from our Seattle headquarters to the Chicago distribution
> center. What options do I have?"

Then:

> "Which option fits the policy available to you, and would I need approval?"

The caller is a fixed demonstration service persona with an EMEA policy scope,
not the signed-in presenter's delegated identity. State that scope before
discussing the policy; do not imply that the Seattle-Chicago route determines it.

Use the exact baseline version recorded by the experiment to show its failed
case. Explain the actual failure, for example a city name being passed where a
location identifier was required, or an unrelated route being substituted.
Do not manufacture a new outage in the accepted runtime.

In Foundry's agent **Traces** view, search for the full response ID from the
baseline experiment, confirm the version and execution time, and open that
trace. In **Trajectories**, select the route-search tool span and its
**Input + Output** tab. A technically completed request may have no error span;
do not restrict this quality investigation to execution errors.

Alternatively, from Azure Monitor select the affected agent and
**View Traces with Agent Runs**, then open the matching execution's
**End-to-end transaction details** and its simple view. Identify:

1. The question and answer, where fictional-content recording permits it.
2. The actual model and tool operation.
3. The tool arguments/result that explain the incorrect outcome.
4. Latency and token usage for that execution.

Expected narration:

> "The request completed technically, but it did not solve the task. The trace
> explains the failure; the business evaluation is what prevents us calling
> this a good release."

Do not claim backend parent-child correlation merely because two spans share a
workspace. Show the actual linked transaction. Distinguish the remote tool call
from its backend span: those can represent the same operation, not two separate
business actions. See
[agent transaction investigation](https://learn.microsoft.com/azure/azure-monitor/app/agents-view#monitor-your-ai-agents).

## 8-11 minutes: show the external runtime

<a id="primary-demo-part-2-contoso-field-in-five-to-seven-minutes"></a>

Open the prepared Field transaction from Application Insights
**Agents (Preview)**. Use the simple view to show the invoke-agent span, model
calls, and tool executions together. Confirm `gen_ai.agent.id` and the execution
time against the private rehearsal evidence. The business question is:

> "Summarize work order WO-00015, including its customer, product, and site."

Identify the work order, customer, product, and site tools. Tie this trace to the
fresh execution captured by the rehearsal, not an arbitrary old success.
Field runs Pydantic AI in an internal Azure Container App; the rehearsal invokes
the runtime inside that deployed container. It is not a demonstration of public
HTTP ingress or end-user delegated authentication.

Say:

> "We added telemetry attribution and an external registration. We did not move
> the application into Foundry or put Foundry in its request path."

External-agent registration is **public preview**. It matches the runtime's
`gen_ai.agent.id` to the registration and does not host, proxy, or invoke it.
Human evaluation, trace-to-dataset conversion, and red-team targeting are not
currently supported for this registration type.
[Supported contract and limitations](https://learn.microsoft.com/azure/foundry/agents/how-to/register-external-agent)

## 11-15 minutes: prove an enforcement boundary

Use the two requests captured by `python -m contoso_foundry.demo gateway --run`.
The first omits the Gateway subscription credential; the second supplies the
existing credential securely.

| Request | Required observation |
| --- | --- |
| No subscription credential | HTTP 401; no backend response |
| Valid subscription credential | HTTP 200; successful model response |

In the owned Log Analytics workspace, use the two private
`gateway_request_id` values from the rehearsal. Close **Queries hub** if it
covers the results. Use **KQL mode** to enter the query; leave the results ready
before presenting. Microsoft documents the
[Log Analytics query workspace](https://learn.microsoft.com/azure/azure-monitor/logs/log-analytics-overview).

```kusto
ApiManagementGatewayLogs
| where TimeGenerated > ago(1h)
| where CorrelationId in ("<denied request ID>", "<allowed request ID>")
| project TimeGenerated, CorrelationId, ApiId, OperationId,
          ResponseCode, BackendResponseCode
```

The one-hour filter is for a fresh rehearsal only. For recorded evidence,
replace it with an explicit UTC interval containing both requests, and confirm
the portal's time selector does not exclude that interval:

```kusto
| where TimeGenerated between (datetime(<UTC start>) .. datetime(<UTC end>))
```

Show the exact matching rows, not merely another successful request within the
same time window. `x-contoso-gateway-request-id` is produced by API Management's
own `context.RequestId`; clients cannot choose it through that response header.
See [policy expressions](https://learn.microsoft.com/azure/api-management/api-management-policy-expressions),
[subscription-key authentication](https://learn.microsoft.com/azure/api-management/api-management-subscriptions),
and the [Gateway log schema](https://learn.microsoft.com/azure/azure-monitor/reference/tables/apimanagementgatewaylogs).

Say:

> "This is an actual Gateway access decision. Watching an agent's telemetry
> does not automatically put that agent behind this Gateway."

This scene proves **Gateway subscription access control**, not Entra per-user
authorization, a token-quota rejection, or enterprise-wide blocking of all
agents. Native Travel calls and Field's direct model calls remain distinct.
The [Gateway architecture](platform/ai-gateway.md) explains those routes.

## 15-20 minutes: compare and improve

Prepare the paired comparison before the presentation; do not spend the final
five minutes waiting for a cloud evaluation job.

Run against two existing immutable versions, using the same pinned judge and an
approved reserve within the environment's remaining budget:

```powershell
python -m contoso_travel_agent.experiments `
  --baseline-version "<baseline version>" `
  --candidate-version "<candidate version>" `
  --judge-deployment "<existing pinned judge deployment>" `
  --budget-reserve-usd "<approved reserve>" `
  --output-dir "internal\travel-experiments\<new run label>" `
  --enable-module optional-control-plane --run
```

Omit `--run` to inspect the dataset without cloud calls. The live command records
each turn before continuing and creates one evaluation with two named runs.
It does not deploy, promote, or delete an agent. Interrupted runs retain their
private evidence; a new run requires a new output directory. If both cloud runs
already exist, add `--resume-from "<original timestamped experiment folder>"`
with a new output directory to finish readback without repeating model or grader
calls. The manual
**Travel paired experiment** GitHub workflow runs on protected `main` and
publishes only scanner-approved summaries. Use the local command to retain full
private evidence; hosted-runner private files are ephemeral.

The natural, unknown, and ambiguous location cases require completed resolver
calls for the requested place terms, not just a plausible answer or guessed
canonical IDs. Resolver query wording can vary; canonical route/fare arguments
still match exactly. Changing these requirements changes the dataset digest,
so an older comparison cannot authorize a release against the updated dataset.

In Foundry's evaluation view, select the intended **baseline** and **candidate**
runs, then **Compare**. Explicitly choose the intended run in the comparison's
**Baseline** selector; selecting two runs alone does not establish the correct
direction. Match the run IDs to the private experiment manifest instead of
choosing the first object with a familiar name. Confirm their recorded agent
versions, model deployments, dataset digest, and shared judge. Open the same
business case on both sides.
Microsoft documents [baseline and sample-level evaluation comparison](https://learn.microsoft.com/azure/foundry/how-to/evaluate-results).

Explain the decision using business correctness, observed latency and tokens reported by the Responses API,
and the recorded list-price estimate. Distinguish inference estimates from
total Azure spend and evaluator cost. A small demonstration dataset does not
establish statistically significant production superiority.
If the comparison displays **Too few samples**, say so explicitly rather than
presenting the difference as statistically significant.

Keep these outcomes separate:

| Outcome | Meaning |
| --- | --- |
| Evaluation completed, business criterion failed | A real measured weakness in the candidate |
| Evaluation could not execute | An API, schema, or configuration failure; no quality conclusion |
| Content-safety criterion passed | That criterion passed; it does not establish travel correctness |
| Candidate evaluated successfully | Evidence exists; it is not automatically the routed version |

Point to the release decision and exact accepted route. Immutable version
creation is not promotion. Rollback also requires the previous tool backend,
connection, and identity to remain usable; an old version number alone is not
a recovery plan.

### Release and recovery preparation

Do these operations before the timed presentation. The explicit release command
rereads both cloud evaluation runs, requires every candidate case to pass,
checks both dependency bundles and the expected current route, and invokes the
named endpoint to confirm the selected version. It also checks that endpoint
authentication and protocols remain unchanged.
Immediately before either promotion or rollback, it repeats the exact live
ownership inventory and governance preflight. A historical passing evaluation
cannot authorize a route change while either baseline has drifted. The legacy
`operations evaluate` promotion path uses the same gate. Pass the installation's
enabled modules with `--enable-module` or `FOUNDRY_ENABLED_MODULES`; the
deployment workflow carries its detected optional modules into this gate.

The mutating helper rereads the complete endpoint configuration after the slow
preflights and refuses a changed route, authentication scheme, or protocol.
It then patches only the version selector. This read-then-patch check is **not
atomic compare-and-swap**: serialize routing changes through one operator or
workflow, and do not make concurrent portal changes during release. The
post-update readback detects disagreements but is not a lock.
The [published named-agent API contract](https://github.com/Azure/azure-rest-api-specs/blob/73972b766e47d15a5342c90913a738bb7809ccee/specification/ai-foundry/data-plane/Foundry/openapi3/v1/microsoft-foundry-openapi3.json#L2604-L2675)
does not define an ETag/`If-Match` precondition for this update. Sending an
arbitrary header would not establish server-side protection, so this command
does not claim it and does not automatically retry its route mutation.

```powershell
python -m contoso_travel_agent.release promote `
  --comparison "internal\travel-experiments\<run>\<original experiment>" `
  --expect-version "<currently accepted version>" `
  --output-dir "internal\travel-releases\<new promotion label>" `
  --enable-module optional-control-plane --apply
```

Without `--apply`, it performs no cloud calls or writes. A promotion receipt is
saved before the route change so recovery is possible even if subsequent
verification is interrupted:

```powershell
python -m contoso_travel_agent.release rollback `
  --receipt "internal\travel-releases\<promotion label>\receipt.json" `
  --expect-version "<promoted version>" `
  --output-dir "internal\travel-releases\<new rollback label>" `
  --enable-module optional-control-plane --apply
```

Rollback verifies that the recorded previous backend, connection, image and
scoped permissions still exist. It refuses to overwrite a route changed by
another operator. Re-promote through the original comparison when the rehearsal
is complete; do not leave the older baseline serving unintentionally.
The named endpoint invocation used for readback is an SDK **public-preview**
path. See [Foundry SDK guidance](https://learn.microsoft.com/azure/foundry/how-to/develop/sdk-overview).

Close with:

> "We can see agents across hosts, explain a failed task, identify which
> controls actually apply, and use comparable evidence to decide what to
> release. The agents are the workload; operating them is the demo."

## Rehearsal acceptance

The demo is ready only when a presenter can complete all five scenes in twenty
minutes without undocumented scripts or unexplained error badges.

- The native estate view includes Travel and Field with actual recent activity.
- A specific baseline business failure can be opened in its trace.
- A fresh external execution is attributed correctly and compute is restored.
- The two Gateway requests match their exact diagnostic records.
- The paired evaluation can be compared, and the accepted route is explicit.

If a scene lacks evidence, record the blocker and repair or explicitly remove
that claim before presenting. An offline fallback can explain a contract but
does not satisfy a live-demonstration acceptance condition.

## Extensions, not prerequisites

[Support](agents/contoso-support.md) and [Research](agents/research.md) add
framework coverage. [Concierge](agents/concierge.md) is an unexported ALM scaffold,
not this demo's front door. [SRE and Approvals](operations/control-plane-coverage.md)
remain optional; a human-review output envelope is not a completed human
approval workflow. See the [reference architecture](architecture/overview.md)
only after the operator story.
