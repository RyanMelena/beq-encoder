# UX guidelines

These guidelines describe the flow that BEQ Encoder should have.
The current app does not follow all of them yet.
Use them for any UI change.
To run an automated UI hill climb against them, see [How to set up a UI hill climb](ux-hill-climb.md).

## The app has three levels

The app has three levels, and each level has its own job.

| Level | What it is for |
|---|---|
| Library | A summary of every film. The user sorts and filters it to find a film. |
| Film detail page | Everything the app knows about one film. The user reads it and starts work from it. |
| Wizard | A guided path for one film. It adds a BEQ track to the film, or it makes a clip of the film with BEQ tracks. |

Almost every visit ends in the wizard, because the usual goal is to give a film a BEQ track.
The user chooses the output before the wizard opens, by clicking **Add BEQ track** or **Make a BEQ clip** on the film detail page.
The wizard then asks for three decisions, always in this order.

1. The user confirms the audio track. The app preselects the best track, and most users keep it.
2. The user picks a filter and sets its low-end cutoff. This step is the reason for the visit.
3. The user confirms the options and starts the work.

After the third decision, the app works without the user.
The user then wants to know two things, whether the work is finished and whether it succeeded.

## Show what the app knows before the user asks

The BEQ Catalogue is a local database.
The app knows which of its filters match a film as soon as it scans the film.
It knows each filter's curve, author, notes, and bass restored.

- When the user opens a film, show its filters at once on the film detail page. Do not start a job first.
- Show the number of filters on each film's Library row, so that the user sees it before opening the film.
- Start slow work only when the user asks for its result. Slow work is designing a filter, estimating the level change, and encoding.
- Never make the user wait for slow work to see what the app already knows.

## Put everything about a film on its detail page

The Library row is a summary.
The film detail page holds what does not fit in a row.
Opening a film from the Library opens this page, and it does not start the wizard.

- Show every part of the film, which are its identity, file, audio tracks, filters, candidate rating, activities, and history.
- Show each filter with its origin, its curve, and its bass restored. Mark the filters that are in use.
- Show each BEQ track with the filter it was made from and its before and after charts.
- Show the logs of the film's activities and of its finished work.
- Link to what lives outside the app, such as a filter's BEQ Catalogue page and the film's TMDB page.
- Put the file tools here, such as **Rescan file**.
- Give the page two buttons that start the wizard. **Add BEQ track** is the primary button. **Make a BEQ clip** is the secondary button.
- Let the user start the wizard from a filter in the list. The wizard then opens with that filter selected.

## Make adding a BEQ track a wizard

The wizard shows three steps in order, which are Track, Filter, and Options.
The current step is open.
Each other step shows a one-line summary of its choice.

The button that opens the wizard sets its output.
**Add BEQ track** opens the wizard to add one BEQ track to the film.
**Make a BEQ clip** opens the same wizard to make a clip.
One wizard serves both outputs, so that a clip and a film get the same track, filter, and options.

- Show the output in the wizard's heading, such as "Add a BEQ track to Klaus" or "Make a BEQ clip of Klaus", with a link that switches to the other output.
- Give each step one primary button, and name the button for what it does next, such as **Continue to options**, **Add BEQ track**, or **Make BEQ clip**.
- Set a default at every step. With the defaults, the user picks a filter and clicks **Add BEQ track**.
- Keep the user's choices when the user goes back a step or switches the output. When the user leaves and returns, reopen the step that the user left.
- Put a rare choice inside the step that it belongs to, collapsed. Rare choices are designing a filter with BEQ Forge when other filters exist, using another track, and keeping both BEQ tracks. Do not give a rare choice its own page or mode.
- When the user finishes or cancels the wizard, return to the film detail page.

### Filter step

- Show every filter with the same facts, which are its origin, its curve, its bass restored, and its level change.
- To add a BEQ track, let the user select one filter.
- To make a clip, let the user select several filters. The clip holds the original track and one BEQ track for each selected filter, and the user compares them by switching tracks in a player.
- Put the low-end cutoff in this step. The low-end cutoff removes bass below a frequency that the user sets, so that inaudible bass does not force the rest of the track down.
- When the user changes the low-end cutoff, update each filter's curve, bass restored, and level change at once. The user compares filters on the figures that the BEQ track will have.
- Take the default low-end cutoff from the Settings page. The right value depends on the user's subwoofer, so the user sets it once and changes it here only for one track.

### Options step

The Options step shows only the options for the wizard's output.

- To add a BEQ track, show the output format, the level change, whether the BEQ track becomes the default track, and whether it replaces an existing BEQ track.
- To make a clip, show the start time and the duration. Show the clip folder, and do not ask for a path.
- Suggest a start time at the film's heaviest bass passage, and let the user pick another passage from the bass-over-running-time chart.
- Say in this step what the work changes. Adding a BEQ track changes the film file. Making a clip does not.

### Clips

A clip is a short copy of the film with BEQ tracks, so that the user can hear a filter before committing to it.

- Give each BEQ track in a clip the level change that the full track would get. A level change measured from the clip alone would make the clip play at a different level from the real BEQ track.
- After the app makes a clip, show where the clip is and offer **Add BEQ track** for each filter in the clip. The wizard then opens with the same track, filter, and low-end cutoff.
- Treat a clip as disposable. Do not list clips on the film detail page, and do not track a clip after the app makes it.
- Take the clip folder from the app's configuration. The clip folder must be outside every library folder, because the scanner would list a clip in a library folder as a film.

## Keep jobs out of the user's way

A job is how the app does slow work.
Users think about films, and a job is an implementation detail to them.

- Let the user make every decision in the wizard. The user never opens a job to decide something.
- Show progress and results on the film detail page and on the film's Library row.
- Make the Jobs page an activity log, and name it Activity. It lists what is running, what is queued, what finished, and what failed.
- Do not use a job status that waits for the user. When BEQ Forge finishes a filter, add the filter to the film's filter list. The user chooses it in the wizard.
- Do not show a status that the user must open another page to understand.

## Run slow work in the background

- Offer BEQ Forge inside the Filter step, with its duration, such as "Design a filter for this track, which takes a few minutes".
- Add the new filter to the list when it is ready. The user can pick another filter in the meantime.
- Load level-change estimates in the background and fill them in as they arrive. The user never waits for an estimate to choose a filter.
- For any work longer than a few seconds, show the expected duration and the overall progress, and let the user leave the page.

## Call each thing by one name

Give each thing one name, and use that name in every label, status, and message.
Two names for one thing make the user look for two things.

| Name | What it means |
|---|---|
| Film | The movie that the user works on. |
| File | The film's file on disk. Use this word only for the path, scanning, and read errors. |
| Audio track | One audio track in the file. Name it by its number, format, layout, and language. |
| BEQ track | An audio track that the app made by applying a filter. |
| Filter | A bass EQ curve that the app can apply to an audio track. Every filter is the same kind of thing, whatever its origin. |
| Origin | Where a filter came from. The origin is BEQ Catalogue, with the author's name, or BEQ Forge. |
| BEQ Catalogue | The community database of filters. |
| BEQ Forge | The tool that designs a filter for a track. |
| Candidate rating | How likely the film is to gain from a BEQ track. |
| Low-end cutoff | The frequency below which the app removes bass when it applies a filter. Do not call it a high-pass filter on screen, because "filter" already has one meaning. |
| BEQ clip | A short, disposable copy of a film with BEQ tracks, for listening tests. Write "BEQ clip" on screen. This document shortens it to "clip". |
| Activity | Work that the app is doing for a film now. |

Do not name filters by kind, such as "catalogue filter" or "designed filter". Say "filter", and show the origin as a fact about it.
BEQ Catalogue and BEQ Forge are product names, so show them as written.
Put BEQ Forge's version and its design method under technical details, along with pipeline stage names.
Do not show the word "job" outside the activity log and technical details.

## Model a film as a file with tracks, filters, and activities

A film is the thing that the user works on.
From the app's side, a film has these parts.

| Part | What it holds |
|---|---|
| Identity | The title, year, edition, and TMDB id. The app matches filters from the BEQ Catalogue with these. |
| File | The file on disk, its path, when the app last scanned it, and whether the app can read it. |
| Audio tracks | Every audio track in the file, including each BEQ track. |
| Filters | Every filter that the app knows for the film, each with its origin. |
| Candidate rating | How likely the film is to gain from a BEQ track, judged from its original audio. |
| Activities | The work that the app is doing for the film now. |
| History | The finished jobs and their results, including the before and after charts. |

Show each part as itself.
Do not fold several parts into one status label, because one label cannot say what the file holds, what exists, and what is running.

### Audio tracks

- List every audio track with its number, format, layout, and language. Use that name for the track everywhere.
- Show a BEQ track as a track. Say which filter it was made from, its low-end cutoff if it has one, and which track was its source.
- Keep the name that the app writes into the file for a BEQ track short. Record the filter, the low-end cutoff, and the other details in the track's tags, and show them on the film detail page.
- Mark the track that the app would use by default, and say when a track cannot get a BEQ track.
- A BEQ track does not finish a film. The user can add another BEQ track or replace one.

### Filters

Each film has one list of its filters.
The list treats every filter the same way.
Each filter shows its origin, its curve, its bass restored, and its level change, so that the user compares filters on the same facts.

- Say which track each filter was made for.
- Mark a filter as in use when a BEQ track in the file was made from it. Derive the mark from the BEQ track, and do not store it on the filter.
- Show the mark with a color and also a tick or a label, because color alone excludes some users.
- Keep an in-use filter in the list after the catalogue revises or withdraws it, or after a newer design replaces it. Label it "revised since" or "no longer in the catalogue".
- Leave out catalogue entries that have no filter bands.

### Candidate rating

The candidate rating comes only from the bass check of the film's original audio tracks.
The rating does not change when the user adds a BEQ track or when a filter appears.
Each original track keeps its own bass-check result.
The film's rating is the result for its default track.

| Rating | Meaning |
|---|---|
| Strong | The bass check found clear roll-off. |
| Possible | The bass check found some roll-off. |
| Unlikely | The bass check found no roll-off. |
| Unknown | The bass check has not run, or the track has too little bass to judge. The label says which. |
| Not supported | The film has no track that the app can make a BEQ track from. |

### Activities

An activity is work that the app is doing for the film now.
Most films have none, and a film can have more than one.
An activity is queued or running, and a running activity shows its overall progress.
When an activity ends, it moves to the film's history with its result, which is a new BEQ track, a new filter, or a failure.

## Let the user sort and filter the Library

The Library is one list of films.
The app does not decide which film comes first.
The user sorts and filters the list to choose what to work on.

- Show these columns for each film: the title, the candidate rating, the number of filters, the number of BEQ tracks, and the activities.
- Let the user sort by any column.
- Let the user filter by any column, and combine filters. For example, the user filters to films with a Strong candidate rating and no BEQ tracks.
- Show the active sort and the active filters above the list, with one control that clears them.
- Keep the user's sort and filters when the user opens a film and comes back.
- When the user opens a film from the list, open its film detail page.
- Let the user select several films and add a BEQ track to all of them with the defaults.

## Keep maintenance on its own page

Put scanning, catalogue refresh, system status, and logs on a Settings page.
The Settings page also holds the default low-end cutoff and shows the clip folder.
Give the Library at most one small link to that page.
The app scans without the user.
Manual scanning is a recovery tool, so it does not belong in the path.

## Give every ending a next step

- After a clip, show where the clip is and offer to add the same BEQ track to the film.
- After a success, show the result and offer a link back to the Library, with the user's sort and filters kept. The result includes the before and after charts and the level change in plain words.
- After a failure, say in one sentence what happened, say what the app did not change, and offer the most likely fix.

## Patterns to avoid

This app has shipped each of these patterns.

- The user starts a job to see a list that the app already has.
- Each pipeline object has its own page, and one task sends the user between the film page, the job page, and the Jobs list.
- Two buttons differ in a way that the user cannot judge yet, such as **Find filters** and **Design a filter**.
- A status is named after an internal stage.
- A status sentence repeats the column next to it.
- A paragraph under a button explains the button. A button that needs a paragraph points to a flow that needs a redesign.
- A confusing screen gets more words instead of a simpler flow.

## Apps to learn from

Bazarr and Radarr are the closest models.
Combine Bazarr's manual search with Radarr's Interactive Search.
The user opens a film and sees every candidate filter at once, with the facts needed to compare them.
The user picks one, and the rest happens in the background.

| App | Why it is similar | What to take from it |
|---|---|---|
| Bazarr | It adds a community-sourced subtitle track to films that the user already has. | Its home view is a Wanted list. Its manual search lists every candidate for a film with a score, and one click applies a candidate. |
| Radarr and Sonarr | They manage a self-hosted library with a background queue. | Interactive Search shows candidates in a table. Activity is a log. Library rows carry status icons. Rows support bulk selection. |
| ezBEQ | It uses the same catalogue and has the same users. | It browses the catalogue by title and author, and it applies an entry in one step. Its users already know its words. |
| MusicBrainz Picard | It matches local media to a community database, and then writes to the file. | It shows a confidence for each match. It writes nothing until the user saves. It shows what will change before it changes. |
| HandBrake | Its flow is source, then preset, then queue. | Presets act as defaults. A summary shows what will happen before the work starts. The queue runs without the user. |
| Tdarr and Unmanic | They process audio and video across a library on a home server. | They show overall progress and a history for each file. Problem views are separate from the main list. |
| Plex Fix Match | It picks the right candidate for one film. | A dialog shows the candidates and takes one choice. The user then returns to the previous page. |
