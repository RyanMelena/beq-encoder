# UX redesign decisions

The [UX guidelines](ux-guidelines.md) leave these questions open.
Each section states the decision and the reason for it.
The owner can overrule any of them.
The [build spec](ux-build-spec.md) describes the pages that follow from them.

## 1. The wizard is its own page

The wizard is a page, at `/films/{id}/add` to add a BEQ track and at `/films/{id}/clip` to make a BEQ clip.
The query parameters `step`, `track`, `filter`, and `cutoff` open the wizard at a step with choices made, so a filter on the film detail page links straight to the Options step.

A page has an address, works with the browser's Back button, and fits a phone.
Radarr's Interactive Search opens over the film page, but it holds one table.
The wizard holds three steps with charts, and a dialog that size does not fit a phone.

The film detail page moves from `/media/{id}` to `/films/{id}`, because the guidelines call the thing a film.
`/media/{id}` redirects to the new address.

The wizard shows its three steps on one page, with one step open.
It opens on the Filter step, because the Track step has a default.
With a filter preselected, adding a BEQ track takes three clicks from the film detail page: **Add BEQ track**, **Continue to options**, **Add BEQ track**.

The app keeps the user's choices for each film in the database, in a `drafts` table.
The choices survive a step back, a switch of output, and a return another day.

## 2. The film detail page puts filters first

The parts appear in this order.

1. The heading: the title, the candidate rating, the TMDB link, **Add BEQ track**, and **Make a BEQ clip**.
2. Activities and the latest result. This part appears only when the film has one.
3. Filters.
4. Audio tracks, with each BEQ track's filter, source track, level change, and charts.
5. Candidate rating, with the bass-check figures.
6. History.
7. File, with the path, the last scan, and **Rescan file**.

Filters come first because they are the reason for most visits.
Every part is open except three: a BEQ track's charts, history entries after the third, and the File part.
Those three are for looking something up.

## 3. Job pages redirect to the film

No job page remains.
`/jobs` redirects to `/activity`.
`/jobs/{id}` redirects to the film detail page at that job's entry, `/films/{film}#job-{id}`.
When the film is no longer in the Library, `/jobs/{id}` redirects to `/activity#job-{id}`.
A job's log and charts keep their addresses under `/jobs/{id}`, because history entries and BEQ tracks link to them.

The `awaiting`, `no_beq`, and `unsupported` statuses are not used for new jobs.
A job is queued, running, done, failed, or cancelled.
At startup the app closes every job that was waiting for the user and moves its BEQ Forge filters to the film's filter list, so no filter is lost.
Old jobs with a retired status stay in the history as finished jobs.

A job does one of three things: it designs a filter with BEQ Forge, it measures level changes, or it adds a BEQ track.
The `prepare` phase is removed.
The catalogue lookup that it did is a database read, so the film detail page does it.

## 4. The app measures five preset cutoffs ahead, and a custom cutoff when it is set

The level change needs the whole track decoded.
The decode takes minutes for a film.
After the decode, measuring one filter at one cutoff takes about one second for a two-hour, eight-channel track.
The measurement ran in the test image on synthetic audio.

The app starts a measuring job when the user opens the wizard for a track whose filters have no figures.
Opening the wizard is how the user asks for the result, so the app does not decode every film in the Library after a scan.
The job measures every filter at the preset cutoffs, off, 5, 10, 15, 20, and 25 Hz, and at the default from the Settings page.
The app stores the figures in a `levels` table.

The Filter step offers **Choose cutoff**, a list of the presets, and **Custom cutoff**, any value up to 40 Hz.
A custom cutoff redraws the curves at once.
Its level change is measured when the user sets it, by a measuring job for that cutoff only.
Once the track has been read, that job takes a few seconds.

Three rules follow.

- Each filter in the wizard shows "Measuring" until its figure arrives, and the page fills it in without a reload. The user can choose a filter and start at any time. Starting cancels the measuring job, because nobody needs its figures any more.
- A change between presets updates the curve, the bass restored, and the level change at once, because every preset is already measured.
- A film measures one thing at a time. A custom cutoff set while the presets are measured waits for them, and the wizard then asks for it.

A BEQ Forge job measures its new filters before it ends, while the decoded audio is at hand.

A BEQ clip takes each filter's full-track figure from the `levels` table.
If the table has no figure yet, the clip job measures the full track itself before it encodes.
If that measurement fails, the clip fails and says why.
A clip at its own level would play louder or quieter than the BEQ track it previews.

## 5. The candidate rating belongs to the film

The film's rating is the bass-check result for its default track, as the guidelines say.
The Library shows that one rating for each film.
The film detail page shows the bass-check result beside each original track.

The bass check runs only on the default track today.
Other tracks show "Not checked".
Running the bass check on every track is not built.

A catalogue filter does not raise the rating.
The Library's Filters column says that a filter exists, and the user can filter on both columns.

## 6. A film can have several activities, and they run one at a time

A film can have one activity of each kind at once: one BEQ Forge design, one measurement, one BEQ track being added, and any number of clips.
The queue never runs two jobs for the same film at the same time, because adding a BEQ track replaces the file that the others read.
A clip does not start while a BEQ track is being added to its film.

## 7. The BEQ track name is at most 40 characters

The default name is `BEQ [{gain}] {codec}`, for example `BEQ [-4.5 dB] TrueHD Atmos 7.1`.
The app cuts a longer name at 40 characters, on a word boundary.
The gain comes first, so a player that shortens names still shows it.

The filter's origin and author, the low-end cutoff, the source track, and the filter bands were already written to the track's `BEQ_*` tags.
The name no longer repeats them.
The film detail page reads them from the tags.
A custom `TRACK_NAME_TEMPLATE` still works, with the same limit.
Existing BEQ tracks keep their names, and the app reads them as before.

## 8. Other decisions

**The Settings page saves the default low-end cutoff.**
The guidelines say that the user sets it once.
The app stores it in the database.
Until the user saves a value, the default is `BEQ_HIGHPASS_HZ`, so an existing deployment keeps its cutoff.
The clip folder stays in the app's configuration, as `CLIP_DIR`, and the Settings page shows it.

**The Library remembers the sort and the filters in a cookie.**
Opening `/` with no parameters restores them.
**Clear** removes the cookie.
The old `?show=` addresses redirect to the matching filters.

**Bulk add skips films that have no default.**
**Add BEQ track to selected** uses the default track, the first filter made for that track, and the default options.
A film with no such filter is skipped, and the Library names the skipped films.
The app does not start BEQ Forge in bulk, because a design needs a choice afterwards.

**A filter for another track is listed, collapsed.**
The film's filter list holds every catalogue filter for the film.
Each filter says which of the film's tracks it was made for, or which release.
In the wizard, filters that were not made for the chosen track sit under a collapsed heading, and none of them is preselected.

**BEQ Forge filters belong to the file they were designed from.**
The app stores the file's size and modification time with each BEQ Forge filter and each level figure.
When the app adds a BEQ track, it carries them over to the new file.
When the file is replaced from outside, they no longer apply, and the app hides them unless a BEQ track uses one.

**BEQ Forge's answer of "nothing to restore" is a finished job.**
The history says that BEQ Forge found no roll-off and designed no filter.
The rating does not change, because it comes from the bass check.

**The clip's start chart comes from the measuring job.**
The measuring job stores the bass level over the running time.
The Options step of a clip draws it and suggests the heaviest passage.
Before the measurement arrives, the step says that the app picks the heaviest passage when it makes the clip.

**The catalogue search is not built.**
The old job page could search the whole catalogue for a filter listed under another film.
The wizard lists every catalogue filter for the film, including those for other mixes, so the search is rarer than before.
If it comes back, it belongs collapsed inside the Filter step, and a filter it finds joins the film's filter list.

**A job's row says it ended only after the film's row is up to date.**
After an encode, the app rescans the file and stores what the job found before it marks the job done.
A page that sees the job done then also sees the new BEQ track and the in-use mark.

**The film detail page leads with one result, and History leaves it out.**
A failure leads until the next work for the film ends, and a success leads for a day.
History lists the other finished jobs, and it leaves out measurements and clips.

**The Library's default sort is by title.**
The guidelines say that the app does not decide which film comes first.

**The Library follows Debarr's Media page.**
One bar holds the candidate ratings as chips with counts, a menu each for filters, BEQ tracks, and activity, and a search box.
The search updates the list as the user types, and every word must match the title, the year, the edition, or the file path.
The rating chips are one choice, and the menus combine with them and with the search.
Each chip counts the films that the other filters and the search match.
Up to 960 px wide, each row is a card, and a **Sort by** menu with a named direction, such as *A to Z*, replaces the column headings.

**The clip's window is one sentence and a slider.**
The Options step for a clip reads "Create clip from [start] with [duration] second duration".
A window over the bass-over-time chart shows the same clip. Dragging it, clicking the chart, or the arrow keys move the start, and the fields and the window stay in step.
The step recommends a window: the heaviest bass passage for the clip's duration, once the level changes are measured.
A dashed outline marks it on the chart, and the start begins there.
When the user moves the window or changes the duration, **Use the recommendation** appears and puts the window back on the recommendation for the current duration.

**A filter says when it last changed.**
A catalogue filter shows the date the BEQ Catalogue last updated it, or added it.
A BEQ Forge filter shows the date it was designed.

**The old `/system` page redirects to `/settings`.**
