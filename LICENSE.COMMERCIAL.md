# EQForge Commercial License

This file defines `LicenseRef-EQForge-Commercial`, the identifier that appears
in the `SPDX-License-Identifier` line of EQForge source files.

```
SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-EQForge-Commercial
```

That `OR` means EQForge is **dual-licensed**, and you may choose either set of
terms. This document describes the second one. It is a summary for orientation,
not the license itself: the written agreement signed by the maintainer governs.

## The default: GPLv3

Unless you have obtained a commercial license, EQForge is provided under the
**GNU General Public License v3.0** — see [LICENSE](LICENSE). That license is
free of charge and gives you the right to use, study, modify and redistribute
the software, provided your derivatives and redistributions remain under GPLv3
and their corresponding source stays available.

For most people — individuals, community projects, internal tooling, and
open-source projects such as Ardour and PipeWire — GPLv3 is the right and only
option needed, and nothing further is required.

## Who needs a commercial license

> If you are a commercial organization and wish to integrate this DSP engine
> into a proprietary (closed-source) product or hardware solution, you must
> obtain a commercial license from the project maintainer.

Concretely, a commercial license is required if you intend to:

- ship EQForge, or a work based on it, inside a **closed-source application,
  plugin, SDK or library** you distribute;
- embed the DSP core in **hardware or an appliance**, including devices where
  the end user cannot reinstall or modify the software;
- distribute EQForge under terms **incompatible with GPLv3**, or as part of a
  product whose own license does not permit GPLv3 obligations to pass through;
- redistribute it **without offering the corresponding source** of your
  combined work.

A commercial license removes those copyleft obligations for your product, so
you can integrate the native core and ship on your own terms.

## What a commercial license covers

Terms are negotiated per use, and typically address:

| Area | What is agreed |
|---|---|
| Scope | Which components (libeqforge core, PipeWire module, JACK host, Python control plane) and which products |
| Distribution | Shipments, embedded devices, SaaS/internal deployment |
| Term | Perpetual or subscription; whether updates and support are included |
| Support | Response times, engineering assistance, porting help |
| Attribution | How EQForge is credited in your product and documentation |
| Patent & warranty | Any additional warranties beyond GPLv3's "as is" basis |

## What never needs one

- Personal use, and use inside a company or institution (no redistribution).
- Evaluation, benchmarking, prototyping and academic work.
- Integration into **open-source** software under GPLv3 or a compatible license.
- Contributing to EQForge itself.

If you are unsure whether your use requires a commercial license, ask before
shipping — that question is welcome and is not a commitment.

## How to obtain one

Contact the project maintainer:

- **Email:** [erfanjaster@gmail.com](mailto:erfanjaster@gmail.com) — licensing
  inquiries are treated as confidential
- **GitHub:** open an issue labelled `licensing` for general questions that do
  not need to be private

Please include: what you are building, which components you intend to use, how
you plan to distribute it, and roughly what volume you expect. That is enough
for a first answer.

## Two clarifications

**Donations are not licenses.** Contributing money to the project supports
development and confers no rights beyond those GPLv3 already grants. See
[README → Support the project](README.md#support-the-project).

**Contributions are dual-licensed.** Because the project is offered under both
licenses, contributions must be too. By submitting a patch you agree that it
may be distributed under GPLv3 *and* under the commercial license. Substantial
or ongoing contributors may be asked to confirm this explicitly in writing.

---

*This summary is provided for convenience and is not legal advice. In the event
of any inconsistency, the text of [LICENSE](LICENSE) and the signed commercial
agreement prevail.*
