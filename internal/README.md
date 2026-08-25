# internal/

Evidence that names something real lives here, and only here.

Everything in this directory except this README is **gitignored** and is
**excluded from the MkDocs site**. It is the one place where a report may
contain a tenant ID, a subscription ID, an `onmicrosoft.com` domain, a resource
ID, or a principal object ID.

## What lands here

| File | Written by | Contains |
| --- | --- | --- |
| `tenant-state.json` / `.md` | `foundry discover` | Subscription, tenant, signed-in principal, licence SKUs |
| `region-selection.json` / `.md` | `foundry regions` | Full per-region elimination trace, including quota readings |
| `boundary.json` | `foundry boundary` | Live resource-group and role-assignment lookups |

## The rule

Each of those commands writes **two** artifacts: the identifier-bearing one
here, and a sanitized sibling that is safe to publish. The sanitized version
goes through `contoso_foundry.sanitize`, which replaces each identifier with a
stable placeholder so that two mentions of the same tenant still read as the
same tenant without disclosing which tenant it is.

If you find yourself wanting to copy a line out of this directory into `docs/`,
run it through the sanitizer instead. `foundry scan site` will catch you if you
do not, but it is cheaper to not need catching.

## Why this is not just `.gitignore`

`.gitignore` stops an accidental commit. It does not stop an accidental
*publish*: MkDocs would happily render a Markdown file from anywhere its
configuration pointed at. So the boundary is enforced twice —

1. `.gitignore` keeps the files out of the repository, and
2. `foundry scan site` reads the generated `site/` directory and fails the
   build if any identifier pattern survived into the HTML.

The second check is the one that matters, because it inspects the artifact that
would actually be served rather than the sources it came from.
