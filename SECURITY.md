# Security policy

## Supported versions

Security fixes land on the latest released version. Fluvo is pre-1.0 and moves fast;
there is no long-term support branch, and backports to older releases are not offered.

| Version | Supported |
|---|---|
| latest release on [PyPI](https://pypi.org/project/fluvo/) | ✅ |
| anything older | ❌ upgrade first |

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Use GitHub's private vulnerability reporting, which is enabled on this repository:

👉 **[Report a vulnerability](https://github.com/GetFluvo/fluvo/security/advisories/new)**

That opens a private advisory visible only to you and the maintainers. It is the
preferred channel because it keeps the report, the discussion, the fix and the CVE in
one place.

If you cannot use GitHub, email **info@getfluvo.com** with `SECURITY` in the subject.
Please do not include live credentials or client data in either channel.

### What to include

- What an attacker can do, and what access they need to start.
- The affected version (`fluvo --version`) and how you installed it.
- A minimal reproduction. A failing command is worth more than a description.
- Whether the issue involves a connection file, a credential, or data a run writes to disk.

### What happens next

| Step | Target |
|---|---|
| Acknowledgement that a human has read it | **3 business days** |
| An initial assessment — confirmed, not reproduced, or not a vulnerability | **10 business days** |
| Fix or published mitigation for a confirmed issue | depends on severity; we will tell you the plan rather than leave you waiting |

These are targets for a small maintainer team, not a contractual SLA. Commercial
support contracts can carry committed response times — see [SUPPORT.md][support].

We will credit you in the advisory and the changelog unless you ask us not to. We do
not operate a bug bounty and cannot offer payment.

## Scope

In scope: anything in this repository that lets an attacker read or alter data they
should not reach, escalate what a run can do, execute code from data a run reads, or
leak credentials from a connection file, a log, a fail file, or an exported file.

Out of scope, because they are Fluvo working as designed:

- **Code execution from your own project's Python.** A transform script, a `mapper`
  function and a `py:` mapping step are *your* code; Fluvo imports and runs them. Treat
  a `flows.yml` and the files it points at with the same care as a shell script.
- **Credentials in a connection file you chose to store in plaintext.** Fluvo reads the
  file you point it at; protecting it is yours to do.
- **What the Odoo account you authenticate as is allowed to do.** Fluvo cannot grant
  itself more than that account has, and does not try to limit it either.
- Vulnerabilities in Odoo itself — report those to [Odoo](https://www.odoo.com/security-report).
- Findings from an automated scanner with no demonstrated impact.

<!-- github-only -->

[support]: https://github.com/GetFluvo/fluvo/blob/master/SUPPORT.md
