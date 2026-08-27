# Contoso Concierge solution source

This directory is the tenant-neutral unmanaged source contract for the
`ContosoConcierge` Power Platform solution. DEV is the only authoring
environment. TEST and PROD receive the same managed ZIP; neither environment is
an authoring source.

The checked-in files intentionally contain no live environment URL, connector
ID, connection ID, Application Insights connection string, tenant identifier,
or exported component GUID. The first authorized DEV export must be unpacked
over `src/` with `pac solution unpack --packagetype Both`, reviewed, scanned,
and committed. Until that export exists, this is an honest scaffold rather than
a claim that a Copilot Studio agent has been provisioned.

`pac solution pack` can then build the accepted managed artifact named in
`config/concierge/alm.yaml`. Deployment settings under
`deployment/concierge/` supply target-specific values at import time. Secret
values remain in the deployment environment's protected secret store, never in
those files.
