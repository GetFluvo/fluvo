# Getting help

Fluvo is open source under the LGPL-3.0 and free to use. This page says where to ask,
and — honestly — what you can expect back.

## Where to ask

| You want to… | Go here |
|---|---|
| Report a bug, or a run that reconciled wrong | **[File an issue](https://github.com/GetFluvo/fluvo/issues/new)** |
| Ask how to do something, or check whether an approach is sane | **[Discussions](https://github.com/GetFluvo/fluvo/discussions)** |
| Report a security problem | **[SECURITY.md][security]** — never a public issue |
| Propose a feature | An issue, with the migration problem it solves |
| Talk about paid support, a migration, or an assessment | **info@getfluvo.com** |

Before filing a bug, please read
[the documentation](https://fluvo.readthedocs.io/) — especially the reconciliation
contract, which explains most "did it actually work?" questions.

## What a good bug report contains

Migration bugs are hard to reproduce from a description. These five things turn a
report into something fixable:

1. The **exact command**, with credentials removed.
2. The **Odoo version** of the target, and `fluvo --version`.
3. What **reconciliation reported** — the `created + failed + unaccounted == total` line.
4. The **fail file**, or a few representative rows from it, with real values replaced.
5. What you expected instead.

**Please do not attach client data.** A synthetic ten-row file that reproduces the
problem is more useful to us and safer for you, and we will ask for one anyway.

## Response expectations

Community support is **best-effort, from a small maintainer team**. We would rather be
straight about that than publish a number we cannot hold:

- There is **no response-time guarantee** on issues or discussions.
- Security reports do have targets — see [SECURITY.md][security].
- A report with a reproduction gets looked at much sooner than one without, simply
  because it can be acted on.
- Silence means we have not got to it, not that it was rejected. A nudge on the thread
  is welcome.

## Commercial support

If your project needs a response time someone will stand behind, that is a support
contract rather than a GitHub thread. Commercial support can cover a **committed
response target**, help with a migration in progress, and a named contact.

Fluvo itself stays free and LGPL-3.0 either way — paying changes what *we* commit to,
never what the software can do. See the [channel charter][channel charter] for
how we work with implementation partners, including when we will refer work rather than
take it.

Enquiries: **info@getfluvo.com**.

<!-- github-only -->

[security]: https://github.com/GetFluvo/fluvo/blob/master/SECURITY.md
[channel charter]: https://github.com/GetFluvo/fluvo/blob/master/CHANNEL-CHARTER.md
