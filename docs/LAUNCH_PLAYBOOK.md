# Launch playbook

How to introduce Doogle to people who would genuinely benefit, and keep a healthy community
afterwards. Every tactic here is opt-in for the audience and honest about what Doogle is.

## 0. Wholesome rules (read first)

* **No fake stars, upvotes, reviews or sock-puppet accounts.** No buying, trading or "star-for-star".
* **No spam.** One post per community, in the right channel, following that community's
  self-promotion rules (read them first; many subreddits require a 9:1 participation ratio or a
  specific flair/day). No cold DMs or mass @-mentions.
* **Disclose AI-assisted content.** If an assistant helped write code, docs or a post, say so.
  Do not post AI-generated replies in discussions as if they were your own experience.
* **Respect trademarks.** The name is "Doogle", not "Google X". Never use Google logos or brand
  colours, never imply partnership, and keep the non-affiliation line in every launch post:
  *"Doogle is an independent community project, not affiliated with or endorsed by Google LLC."*
* **Be accurate.** Say which blueprints are `syntax-checked` versus `live`. Do not claim
  "one-click" anything or numbers you have not measured.
* **Credit people.** Link contributors and the official docs each blueprint builds on.

## 1. Before launch checklist

- [x] Replace every `OWNER/REPO`, `@OWNER` and `[MAINTAINER: ...]` placeholder (now `rjohnn1/Doogle` / `@rjohnn1`; Code of Conduct contact is GitHub private vulnerability reporting)
- [ ] Repository settings: description, topics (`google-cloud`, `firebase`, `gemini`, `cloud-run`,
      `apps-script`, `cli`, `developer-tools`), website link, Discussions on, private
      vulnerability reporting on, branch protection on `main` requiring CI
- [ ] Labels created from `.github/labels.yml` (see docs/LABELS.md)
- [ ] At least 3 blueprints raised to `live` on a throwaway project, with evidence (GOVERNANCE.md)
- [ ] `demo/doogle-demo.gif` re-rendered from the current CLI
- [ ] Social preview image uploaded (spec below)
- [ ] 5-10 `good first issue`s written with file pointers (e.g. "add `skip_if` to clasp push",
      "routing keywords for X", "new blueprint: Cloud Scheduler + Cloud Run job")
- [ ] First release tagged `v1.1.0` with notes from CHANGELOG.md
- [ ] All CI, CodeQL and freshness workflows green on `main`
- [ ] A pinned Discussion: "What should the next blueprint be?"

## 2. Social preview image spec

Settings > General > Social preview.

* 1280 x 640 px PNG (GitHub's recommended size; keep it under 1 MB), important content inside the
  central ~1100 x 500 px because some sites crop.
* Content: the word **Doogle**, the one-line pitch ("Say what you want to build. See every command
  before it runs."), and a crisp, real terminal snippet (`doogle ask` -> recommended blueprint).
* Neutral palette (dark terminal background, one accent colour such as the `2ea44f` badge green).
  **No Google logos, product icons, or the Google four-colour palette.**
* Small footer text: "Independent open-source project, not affiliated with Google LLC".
* Text at least 40 px tall so it is readable as a thumbnail; check contrast (WCAG AA).

## 3. Where to share (one thoughtful post each, spaced out)

| Channel | Format | Notes |
|---|---|---|
| Show HN | "Show HN: Doogle - dry-run-first blueprints for wiring Google Cloud, Firebase and Gemini" | Link the repo, not a landing page. Post on a weekday morning US time. Stay in the thread for hours and answer critiques directly. |
| r/googlecloud | Text post explaining the problem and asking for feedback | Check the sidebar rules on self-promotion first; lead with what you learned, not "please star". |
| r/Firebase | Focus on `firebase-auth-mvp` | Ask what blueprint Firebase devs want next. |
| dev.to / Hashnode | Tutorial: "From AI Studio prototype to Vertex AI in 10 minutes, with a dry run" | A real walkthrough with the commands; tag `googlecloud`, `firebase`, `opensource`. Disclose AI assistance if used. |
| Google Developer Groups (GDG) | Offer a 15-minute talk or workshop: "Write your first Doogle blueprint" | Contact organisers through official GDG pages; adapt to their format. |
| Google Developer Experts (GDE) community | Ask for review of a blueprint in their area | Ask for feedback, not promotion. Credit reviewers. |
| awesome-lists | PR to relevant lists (e.g. awesome Google Cloud / Firebase / CLI lists) | Only after the project is stable; follow each list's contribution rules exactly; one PR per list. |
| X / LinkedIn / Mastodon / Bluesky | Short demo GIF + one sentence + link | From personal accounts; no paid boosts. |

## 4. Blueprint of the week

A light, sustainable cadence once there are contributors:

1. Monday: maintainers pick one blueprint (new, recently `live`-verified, or community-requested)
   and label its tracking issue `blueprint-of-the-week`.
2. Post in Discussions > Announcements: what it does, its `when_not_to_use`, a `doogle plan`
   excerpt, and credit to its maintainer and contributors.
3. Invite people to run it on a throwaway project and report drift; that is how blueprints earn `live`.
4. Cross-post once to social, never more than weekly. Skip a week rather than post filler.

## 5. Contributor recognition (all-contributors)

* Install the [all-contributors bot](https://allcontributors.org/docs/en/bot/installation) on the
  repository and add a `.all-contributorsrc` and a contributors table to README.
* Recognise every kind of contribution: `code`, `doc`, `blueprint` (custom type or `content`),
  `bug`, `review`, `test`, `ideas`, `question` (helping others in Discussions).
* Thank first-time contributors by name in release notes.

## 6. Roadmap in GitHub Projects

* Create a public Project board "Doogle roadmap" with columns *Ideas*, *Next*, *In progress*, *Done*,
  and fields *Area* (blueprint, engine, docs, community) and *Size*.
* Seed it from [docs/ROADMAP.md](ROADMAP.md); keep ROADMAP.md as the narrative and the board as the
  live status. Link the board from README.

## 7. Releases and release notes

* Semantic versioning (see GOVERNANCE.md). Tag `vX.Y.Z` from green `main`.
* Release notes = the CHANGELOG section, grouped as *New blueprints*, *Blueprint fixes (drift)*,
  *CLI*, *Safety*, *Docs*, *Thanks*, with a verification line ("3 of 5 blueprints `live`").
* Publishing to PyPI is optional; if you do, use trusted publishing (OIDC) from a release workflow,
  never a long-lived token in repository secrets.

## 8. Measuring honestly

Watch what helps users, not vanity numbers: drift issues closed per month, time from drift report
to fix, blueprints at `live`, first-time contributors, and questions answered in Discussions.
Doogle has no telemetry, so do not add any to measure adoption.
