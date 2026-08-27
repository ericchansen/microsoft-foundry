# Connection reference export slot

The authorized DEV export will populate this directory with the
`ccs_FoundrySpecialist` connection-reference definition after its custom
connector exists. A logical name alone is not a valid exported component, so the
repository does not fabricate its connector metadata or component identifier.

TEST and PROD bind that logical name to protected target connections through the
deployment settings examples. Connection IDs and credentials are never
committed. The authorized export must create a real
`src/connectionreferences/**/connectionreference.xml` before packaging is
allowed.
