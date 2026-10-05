# Ops notes (cross-machine, cross-repo)

Shared scratchpad for decisions/status that both Claude Code sessions
(Orion-Station and the laptop) should stay aware of, across repos under
`~/Git/`. Lives here — not loose in `~/Git/` or `~/Documents/` — because a
plain file at those levels is **not git-tracked and will not sync** between
machines. This file rides the same `origin` GitHub sync that `CLAUDE.md`
already uses (see "Machines" in [CLAUDE.md](CLAUDE.md)). If you want this
note visible from a session opened in a different repo (`Terraform_Lustre`,
`OpenStack-Dustoff`), you currently have to open it from here explicitly —
each repo only auto-loads its own `CLAUDE.md`.

**Heavier alternative that already exists but isn't wired up:**
`~/Git/automem` (verygoodplugins/automem) is a real cross-assistant memory
service — FalkorDB + Qdrant + an MCP bridge that Claude Desktop/Code can
connect to directly, designed for exactly this "one memory across tools/
machines" problem. Richard's actual reason for having it: it's the
foundation of a separate project — a personal multi-agent secretary,
codenamed **Ada** (voice, sound, scheduler). That project has its own home
now: **[`~/Git/Ada-Project/CLAUDE.md`](../Ada-Project/CLAUDE.md)** — forked
out on 2026-09-30 so it can be picked up in its own Claude Code session
independent of Job-Assist. Don't duplicate Ada planning here; this plain
file stays Job-Assist's own sync mechanism.

## Hardware inventory (as of 2026-09-30)

Kept here because the Dustoff/Orion split below (CI runner placement) needs
it. Fuller hardware detail + the Ada GPU dilemma now live in
[`~/Git/Ada-Project/CLAUDE.md`](../Ada-Project/CLAUDE.md).

| Machine | RAM | GPU | Role |
|---|---|---|---|
| **Orion-Station** (`192.168.178.40`) | 48GB DDR4 | RTX 3090 (24GB VRAM) | Desktop: gaming, image-gen, Job-Assist's Ollama LLM backend. Also hosts AutoMem. **Actively used for other things — treat its GPU as contended, not dedicated.** |
| **Laptop** (`192.168.178.25`) | 16GB (DDR4?) | RTX 2070-M | Thin client. |
| **Dustoff** | 72GB DDR3-ECC | none (no GPU/NPU) | Kolla single-node OpenStack cloud (see [dustoff-openstack-topology](../../.claude/projects/-home-reseke-Git-Job-Assist/memory/dustoff-openstack-topology.md) memory). Bastion = controller; no external route to tenant nets. |

## 2026-09-30 — CI/CD planning kickoff

Richard wants a GitHub Actions CI/CD setup across repos under `~/Git/`.
Specific tasks not yet fully decided — this is a planning-stage note, not a
build log.

**Repo landscape relevant to this:**

- **`Job-Assist`** (this repo) — pure Python/Flask, no IaC. CI here =
  lint/test + packaging (depth still TBD — see open questions).
- **`Terraform_Lustre`** (GitHub remote: `gogeekness/Dustoff_Terraform`) —
  OpenTofu/Terraform against Dustoff OpenStack.
- **`OpenStack-Dustoff`** (GitHub remote: `gogeekness/OpenStack-Dustoff`) —
  shell scripts + Ansible playbooks/snapshots for Dustoff, plus the
  Slurm-on-OpenStack Tofu+Ansible harness.
- **`Ansible_test`** — local only, **no GitHub remote yet**. Can't have
  GitHub Actions CI until a remote exists. Low priority / sandbox.

**Decisions so far:**

- **All CI/CD here is self-hosted — no GitHub-hosted runners.** Richard has
  the hardware for it (see inventory above); this was an explicit choice,
  not a cost/capability fallback.
- **Runner host split (confirmed 2026-09-30):**
  - **Dustoff** runs the runner(s) for `Terraform_Lustre`, `OpenStack-Dustoff`,
    and any Slurm-harness testing — it has direct access to its own
    OpenStack API and tenant network, no ProxyJump needed.
  - **Orion-Station** keeps AutoMem and the Ollama LLM backend Job-Assist
    already uses. Whether Orion also runs the Job-Assist CI runner, or
    Job-Assist's runner lives elsewhere, is still open (see below).
- GitHub Actions self-hosted runners are per-repo, not shared account-wide,
  unless repos move into a GitHub Organization (not assumed/decided). Plan:
  one physical host can run several runner *service instances*, one
  registered per repo, each its own systemd unit under a dedicated
  low-privilege service account (not Richard's main user) — a self-hosted
  runner executes whatever a workflow file says, so it shouldn't run as a
  privileged account. **Needs confirmation the three GitHub repos are
  private** — a public repo + self-hosted runner is a real remote-code-
  execution risk via fork PRs; not yet confirmed either way.
- Trigger strategy: push/PR to main branches as the baseline, plus
  `workflow_dispatch` (manual) for anything state-changing like a real
  `terraform plan`/`apply`, and room for a `schedule:` cron later (e.g.
  periodic Terraform drift check) if wanted.
- No workflow YAML written yet anywhere.

**Still open — asked, not yet answered as of 2026-09-30:**

- **Ansible test-VM strategy:** ephemeral VMs per CI run (reusing the
  existing Slurm-on-OpenStack Tofu harness — spin up, run playbook twice to
  check idempotency, tear down; no drift) vs. 1-2 static long-lived VMs
  (simpler now, drifts over time, concurrent runs can collide). Richard
  mentioned "possible 1-2 test VMs... pull to github and run testing" —
  leans static, but not explicitly confirmed over the ephemeral option.
- **Job-Assist packaging/CD depth:** full CD (CI restarts the live systemd
  service on its host after tests pass — no registry/artifact step needed
  since the runner and the app would be co-located) vs. CI + a versioned
  GitHub Release artifact with manual deploy vs. lint/test gate only, no
  packaging yet.
- **Terraform `apply` gating:** manual-approval GitHub Environment gate
  (plan auto-runs and posts the diff, apply needs a human click) vs.
  auto-apply on merge to main vs. plan-only in CI, apply stays manual/local
  for now.
- Does `Ansible_test` get a GitHub remote, or does its content move into
  `OpenStack-Dustoff` instead?
- Exact lint/test tooling per repo (e.g. `ruff` vs `flake8` for Job-Assist,
  `ansible-lint` version, whether Molecule is wanted for the Ansible
  convergence/idempotency tests) is still undecided.
- Dustoff's chassis/PSU details (tower vs rack, spare PCIe power
  connectors) — gates the Ada-secretary GPU question above, not the CI/CD
  plan itself, but same box.
