# UX build spec

This spec describes each page of the redesigned web UI: its layout, the data it needs, and the behaviours that a test or a screenshot can confirm.
The [UX guidelines](ux-guidelines.md) are the design.
The [UX redesign decisions](ux-decisions.md) settle what the guidelines leave open.

## Addresses

| Address | Page |
|---|---|
| `/` | Library |
| `/films/{id}` | Film detail page |
| `/films/{id}/add` | Wizard, to add a BEQ track |
| `/films/{id}/clip` | Wizard, to make a BEQ clip |
| `/activity` | Activity log |
| `/settings` | Settings page |

These old addresses redirect.

| Old address | New address |
|---|---|
| `/media/{id}` | `/films/{id}` |
| `/jobs` | `/activity` |
| `/jobs/{id}` | `/films/{film}#job-{id}`, or `/activity#job-{id}` when the film is gone |
| `/system` | `/settings` |
| `/?show=…` | `/` with the matching filters |

`/jobs/{id}/log`, `/jobs/{id}/charts/{kind}`, `/clips/{id}/file`, and `/clips/{id}/log` keep their addresses.

## Data model

`film.py` builds one `Film` from a `media` row, and every page reads it.

| Part | Source |
|---|---|
| Identity | `media.title`, `year`, `edition`, `tmdb` |
| File | `media.path`, `scanned_at`, `error` |
| Audio tracks | `media.probe_json`. A BEQ track's facts come from its `BEQ_*` tags. |
| Filters | The BEQ Catalogue's entries for the film, the `filters` table for BEQ Forge, and the tags of BEQ tracks in the file |
| Candidate rating | `media.rolloff_json` for the default track, and whether any track is supported |
| Activities | `jobs` and `clips` rows that are queued or running |
| History | `jobs` rows that have ended |

New tables:

- `filters` holds each BEQ Forge filter with the film, the track, and the file's size and modification time.
- `levels` holds one measured peak for each film, track, filter, and cutoff.
- `drafts` holds the wizard's choices for each film.
- `settings` holds the default low-end cutoff.

New `media` columns `rating`, `filter_count`, and `beq_count` let the Library sort and filter in SQL.
The app sets them after each scan, each catalogue refresh, and each job.

A job's `phase` is `design`, `measure`, or `encode`.

## Library

### Layout

1. A filter bar: the candidate ratings as chips with counts, a menu each for **Filters**, **BEQ tracks**, and **Activity**, and a search box.
2. A line that states the active sort and filters, with **Clear**.
3. The list. Each row has a checkbox, the title with the file name under it, the candidate rating, the number of filters, the number of BEQ tracks, the activities, and an **Add BEQ track** link.
4. Under the list, **Add BEQ track to selected** and a pager with rows per page.
5. One small link to the Settings page, in the page header.

On a narrow screen, each row is a card, and a **Sort by** menu with a named direction replaces the column headings.

### Data

The list reads `media` with the three new columns and each film's active and last jobs.

### Checks

- [ ] One click on a film's title opens its film detail page, where the filters show. This is the first click count in the hill-climb guide. The target is one.
- [ ] Each row shows the title, the candidate rating, the number of filters, the number of BEQ tracks, and the activities.
- [ ] A click on a column heading sorts by that column. A second click reverses the order.
- [ ] Two filters combine. Strong rating with no BEQ tracks lists only films that match both.
- [ ] The search updates the list as the user types, and every word must match the title, the year, the edition, or the file path.
- [ ] Each rating chip counts the films that the other filters and the search match.
- [ ] The line above the list names the active sort and filters. **Clear** removes them.
- [ ] After the user opens a film and returns to `/`, the sort and filters are the same.
- [ ] **Add BEQ track to selected** queues one job for each selected film that has a default filter, and names the films that it skipped.
- [ ] A film with a running job shows the job's progress in its row.
- [ ] The Library has no scan form and no catalogue button.
- [ ] `/?show=candidates` redirects to the matching filters.

## Film detail page

### Layout

The order is in [decision 2](ux-decisions.md#2-the-film-detail-page-puts-filters-first).

- The heading holds **Add BEQ track** as the primary button and **Make a BEQ clip** as the secondary button.
- Each filter shows its origin, the track it was made for, when it last changed, its curve, its bass restored, and its level change. An in-use filter is marked "In use".
- The activities sit in a panel of their own, above the latest result. Each names its work, links to its entry in the Activity log, and has **Cancel**.
- Each filter has a link that opens the wizard at the Options step with that filter.
- Each BEQ track shows its filter, its low-end cutoff, its source track, its level change, and its charts.
- The latest result appears above the filters: a new BEQ track, a new filter, a BEQ clip, or a failure.

### Data

One `Film`, with its level figures at the default cutoff.

### Checks

- [ ] The page shows the film's filters with no job started.
- [ ] The heading has exactly two buttons that open the wizard, and **Add BEQ track** is the primary one.
- [ ] A filter's link opens the wizard with that filter selected.
- [ ] A filter that a BEQ track was made from is marked "In use".
- [ ] An in-use filter that the catalogue no longer lists stays in the list, labelled "no longer in the catalogue".
- [ ] A running activity shows its overall progress and a **Cancel** button.
- [ ] After a failure, the page says what happened in one sentence, says that the film file was not changed, and offers one fix.
- [ ] After a success, the page shows the level change in words, the charts, and a link back to the Library.
- [ ] After a BEQ clip, the page shows where the clip is and one **Add BEQ track** link for each filter in the clip.
- [ ] The word "job" appears only in the history's technical details.
- [ ] **Rescan file** is on this page.
- [ ] `/media/{id}` redirects here.

## Wizard

### Layout

The heading names the output and the film, with a link that switches to the other output.
The three steps follow in order.
One step is open, and each other step shows a one-line summary with a **Change** link.

**Track.**
The step lists the tracks that can get a BEQ track, with the default marked.
Tracks that cannot get one sit under a collapsed heading, each with its reason.
The button is **Continue to filter**.

**Filter.**
Each filter shows its origin, its curve, its bass restored, and its level change.
The low-end cutoff follows the filters, whose figures and curves it sets.
To add a BEQ track, the user selects one filter.
To make a clip, the user ticks up to four filters.
Filters for other tracks and the BEQ Forge button are collapsed inside the step.
When the film has no filter for the track, the BEQ Forge button is the step's primary button.
The button is **Continue to options**.

**Options.**
To add a BEQ track, the step shows the output format, the level change, whether the BEQ track becomes the default track, and whether it replaces an existing BEQ track.
The button is **Add BEQ track**.
To make a clip, the step reads "Clip starts at [start] and lasts [duration] seconds", shows a window to drag over the bass-over-time chart, and shows the clip folder.
The button is **Make BEQ clip**.
The step says what the work changes.

### Data

One `Film`, the film's draft, the level figures for every offered cutoff, and the bass level over the running time when a measurement has stored it.

### Checks

- [ ] From the film detail page, three clicks add a BEQ track with the defaults: **Add BEQ track**, **Continue to options**, **Add BEQ track**. This is the second click count in the hill-climb guide. The target is three.
- [ ] The wizard opens on the Filter step, with the default track in the Track summary.
- [ ] A filter made for the track is preselected. A filter made for another track is never preselected.
- [ ] The heading says "Add a BEQ track to" or "Make a BEQ clip of", then the film, and its link switches the output and keeps the choices.
- [ ] A change of cutoff changes each filter's curve, bass restored, and level change, with no job started.
- [ ] The chart marks the low-end cutoff and names its frequency. The mark follows a change of cutoff, and Off has none.
- [ ] Pointing at a filter's row, or moving the keyboard into it, highlights the row and picks out its line in the chart. The film detail page does the same.
- [ ] Each figure, the cutoff and the chart explain themselves in a tooltip beside their names, which a keyboard or a tap can open.
- [ ] A filter with no figure shows "Measuring", and the user can still continue.
- [ ] The BEQ Forge button states its duration. After it is pressed, the wizard stays usable, and the new filter joins the list when the design ends.
- [ ] The choices survive a step back and a return to the wizard later.
- [ ] The Filter step's cutoff menu offers the preset cutoffs and a custom one. A custom cutoff redraws the curves and measures only its own level change.
- [ ] The clip's window and its two fields stay in step.
- [ ] The clip's Options step recommends a window for its duration, and offers **Use the recommendation** after the user moves away from it.
- [ ] The Options step for a BEQ track says that it changes the film file. The Options step for a clip says that it does not, and shows the clip folder with no path field.
- [ ] After **Add BEQ track** or **Make BEQ clip**, the browser returns to the film detail page, which shows the new activity.
- [ ] **Cancel** returns to the film detail page.
- [ ] No step has more than one primary button.

## Activity log

### Layout

Four groups in order: running, queued, failed, and finished.
Each row names the work, the film, the status, the progress or the result, and the time.
A row links to the film detail page.
A queued or running row has **Cancel**.

### Data

`jobs` and `clips` rows, newest first.

### Checks

- [ ] The header link says "Activity", with the number of running and queued activities.
- [ ] A row names its work in plain words: "Adding a BEQ track", "Designing a filter", "Measuring level changes", or "Making a BEQ clip".
- [ ] No row has a status that waits for the user.
- [ ] A row's link opens the film detail page.
- [ ] `/jobs` redirects here.

## Settings page

### Layout

1. Default low-end cutoff, with **Save**.
2. Clip folder, shown as text.
3. Library scanning: the library folders, the last and next scan, **Scan now**, **Rescan every file**, and a form that scans one path.
4. BEQ Catalogue: the number of filters, when it was downloaded, and **Refresh**.
5. System status: the encoder, BEQ Forge, and the bass check.
6. Logs: links to the logs of recent jobs and clips.

### Data

The settings, the scanner state, the catalogue, the tool status, and the last twenty `jobs` and `clips` rows that have a log.

### Checks

- [ ] A saved cutoff is the cutoff that the wizard starts with.
- [ ] The clip folder shows, and the page has no field for it.
- [ ] A path that the scan refuses shows its error at the field.
- [ ] `/system` redirects here.

## Journeys for the hill climb

`app/tests/support/serve.py` serves the app on a seeded database.
The seed has level figures, job histories, BEQ clips, and a library folder that exists in the container.
`app/tests/support/journeys.mjs` walks these paths on the seeded app, saves a screenshot of every screen, and counts the clicks.
`app/tests/test_web.py` checks the two click counts against the real routes.

1. See the filters for a film.
2. Add a BEQ track with the defaults.
3. Design a filter with BEQ Forge and add a BEQ track from it.
4. Make a clip that compares two filters, and then add one of them to the film.
5. Recover from a failed encode.
