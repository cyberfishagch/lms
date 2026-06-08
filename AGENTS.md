# lms (cyberfishagch fork of frappe/lms)

Fork of [`frappe/lms`](https://github.com/frappe/lms) (Frappe Learning) with our customizations on top. The upstream `README.md` is the product README — useful for understanding what Frappe Learning does, but not for our fork-specific workflow.

## Remotes

- `origin` → `frappe/lms` (upstream — read-only for us)
- `cyberfish` → `cyberfishagch/lms` (our fork — where we push)

## Branch strategy

- `cyberfish/v2.46.0` — integration branch carrying our customizations on top of upstream v2.46.0. This is the branch `lms-docker/apps.json` pins.
- Feature branches start from `cyberfish/v2.46.0`, PR back into it. Examples in flight: `feat/admin-course-notes`, `fix/auto-issue-certificate-on-completion`.
- **Do not commit directly to `cyberfish/v2.46.0`** — open a PR.
- Upstream tags get rebased onto `cyberfish/v2.46.0` periodically; full procedure in `lms-docker/MATCHBOX_DEPLOYMENT.md` under "When upstream bumps versions".

## Read FIRST

`lms-docker/MATCHBOX_DEPLOYMENT.md` — the 3-repo system, deploy flow, upstream-version-bump procedure. **Local-only commits that aren't pushed to `cyberfish` will not be deployed.**

## How a backend change ships

```bash
# 1. Branch off the integration branch
git fetch cyberfish
git checkout -b feat/your-change cyberfish/v2.46.0

# 2. Make changes, commit
# 3. Push to OUR fork
git push -u cyberfish feat/your-change

# 4. PR into cyberfish/v2.46.0
gh pr create --base cyberfish/v2.46.0 --head feat/your-change

# 5. After merge, rebuild the Docker image
cd ~/repos/lms-docker && ./build-and-push.sh
```

## Where our customizations live

- `lms/lms/api.py` — whitelisted RPC methods consumed by `lms-frontend`. Most backend additions land here.
- `lms/lms/utils.py` — helpers; `get_country_code` (line ~1226) calls `ip-api.com` externally.
- `lms/lms/doctype/<name>/<name>.json` — DocType schemas (PascalCase names like `LMS Course`, `LMS Enrollment`).
- `lms/lms/doctype/lms_batch/lms_batch.py` — Zoom integration. **Not used in production** (no live courses currently — see `lms-frontend/OPERATIONS.md` runbook context).

The user has a global Claude skill `frappe-stack` covering general Frappe conventions (DocTypes, hooks, whitelisted methods, frappe-react-sdk). That's the broader reference; this file is just fork-specific.

## Hard rules

- **Never push to `origin` (frappe/lms upstream).** Push to `cyberfish` only.
- **No direct commits to `cyberfish/v2.46.0`** — PR every change.
- **DocType JSON changes trigger migrations** when the image redeploys. The pipeline runs `bench migrate` before swapping the image; a failing migration stops the deploy and leaves prod on the previous image. Test migrations locally first.

## Related repos

| Repo | What it does |
|---|---|
| **`lms`** _(this)_ | Frappe Learning Python app fork with our customizations. |
| **`lms-docker`** | Builds the bench image with this app baked in (via `apps.json`). |
| **`lms-frontend`** | React SPA consuming `api.py` via `frappe-react-sdk`. |
| **`matchbox-infra`** | CDK for AWS infra (phase 1 scaffold only). |

## Where this file lives

`AGENTS.md` is canonical (tool-agnostic). `CLAUDE.md` is a one-line `@AGENTS.md` pointer so Claude Code picks this up. **Edit this file, not CLAUDE.md.**
